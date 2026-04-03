"""
Smart wallet tracking on Base chain via Alchemy Enhanced API.
Polls tracked wallets for new token buys every 60 seconds.
"""

import json
import os
import time
import requests
from datetime import datetime, timezone, timedelta

from config.constants import (
    ALCHEMY_API_KEY, BASE_RPC_URL, SMART_WALLETS_PATH,
    WETH_ADDRESS, WALLET_4H_RETURN_THRESHOLD,
)
from monitoring.logger import (
    insert_wallet_activity, write_log, token_exists, insert_token,
)
from scoring.engine import update_wallet_buys_cache

# Track last poll timestamp per wallet to avoid re-processing
_last_poll: dict[str, float] = {}


def load_smart_wallets() -> list[dict]:
    """Load tracked wallets from JSON file."""
    try:
        with open(SMART_WALLETS_PATH, 'r') as f:
            data = json.load(f)
        return data.get('wallets', [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_smart_wallets(wallets: list[dict]) -> None:
    """Save wallets back to JSON file."""
    os.makedirs(os.path.dirname(SMART_WALLETS_PATH), exist_ok=True)
    with open(SMART_WALLETS_PATH, 'w') as f:
        json.dump({
            '_meta': {
                'version': 1,
                'last_updated': datetime.now(timezone.utc).isoformat(),
            },
            'wallets': wallets,
        }, f, indent=2)


def get_wallet_transfers(wallet_address: str, from_block: str = 'latest') -> list[dict]:
    """
    Fetch recent token transfers for a wallet using Alchemy's getAssetTransfers.
    Returns list of ERC20 token buys (transfers TO the wallet).
    """
    if not ALCHEMY_API_KEY:
        return []

    # Use Alchemy Enhanced API endpoint
    url = f'https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}'

    # Look back ~4 hours worth of blocks (~7200 blocks at 2s/block)
    payload = {
        'id': 1,
        'jsonrpc': '2.0',
        'method': 'alchemy_getAssetTransfers',
        'params': [{
            'fromBlock': 'latest',  # Will be overridden
            'toBlock': 'latest',
            'toAddress': wallet_address,
            'category': ['erc20'],
            'withMetadata': True,
            'excludeZeroValue': True,
            'maxCount': '0x64',  # 100 most recent
            'order': 'desc',
        }],
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        write_log(f'ALCHEMY | getAssetTransfers error for {wallet_address[:10]}: {e}')
        return []

    result = data.get('result', {})
    transfers = result.get('transfers', [])

    buys = []
    for t in transfers:
        raw_contract = t.get('rawContract', {})
        contract_address = (raw_contract.get('address', '') or '').lower()
        if not contract_address:
            continue

        # Skip WETH transfers (wrapping/unwrapping)
        if contract_address == WETH_ADDRESS.lower():
            continue

        buys.append({
            'contract_address': contract_address,
            'wallet': wallet_address.lower(),
            'amount_eth': float(t.get('value', 0) or 0),
            'tx_hash': t.get('hash', ''),
            'block_num': t.get('blockNum', ''),
            'timestamp': t.get('metadata', {}).get('blockTimestamp', ''),
            'asset': t.get('asset', ''),
        })

    return buys


def poll_all_wallets() -> list[dict]:
    """
    Poll all tracked wallets for new token buys.
    Returns list of new buys found this cycle.
    """
    wallets = load_smart_wallets()
    if not wallets:
        return []

    all_new_buys = []

    for wallet_entry in wallets:
        address = wallet_entry.get('address', '').lower()
        if not address:
            continue

        transfers = get_wallet_transfers(address)
        for buy in transfers:
            tx_hash = buy.get('tx_hash', '')
            if not tx_hash:
                continue

            # Insert into database (INSERT OR IGNORE handles dedup)
            insert_wallet_activity(
                wallet=address,
                contract_address=buy['contract_address'],
                action='buy',
                amount_eth=buy.get('amount_eth', 0),
                tx_hash=tx_hash,
            )

            # Register token if new
            if not token_exists(buy['contract_address']):
                insert_token(
                    contract_address=buy['contract_address'],
                    symbol=buy.get('asset', 'UNKNOWN'),
                    name=buy.get('asset', ''),
                    deployer='',
                )

            all_new_buys.append(buy)

            # Update wallet buys cache for scoring engine
            update_wallet_buys_cache(buy['contract_address'], [buy])

    if all_new_buys:
        write_log(f'WALLETS | Found {len(all_new_buys)} new buy(s) from {len(wallets)} tracked wallets')

    return all_new_buys


def check_wallet_4h_return(wallet_address: str) -> float:
    """
    Calculate a wallet's return over the last 4 hours.
    Used to check if a wallet meets the >50% return threshold (requirement #8).
    """
    # Get recent buys from this wallet
    transfers = get_wallet_transfers(wallet_address)
    if not transfers:
        return 0.0

    # For each token bought, check current vs buy price
    from signals.dexscreener import fetch_single_token

    total_invested = 0
    total_current = 0

    for buy in transfers:
        dex = fetch_single_token(buy['contract_address'])
        if not dex:
            continue

        buy_amount = buy.get('amount_eth', 0)
        total_invested += buy_amount

        # Estimate current value (rough — based on price change since buy)
        # This is a simplified calculation
        current_price = dex.get('price_usd', 0)
        if current_price > 0:
            total_current += buy_amount  # Base case
            h1_change = dex.get('h1_price_change', 0) / 100
            total_current += buy_amount * h1_change

    if total_invested <= 0:
        return 0.0

    return ((total_current - total_invested) / total_invested) * 100


if __name__ == '__main__':
    wallets = load_smart_wallets()
    print(f'Tracked wallets: {len(wallets)}')
    if wallets:
        for w in wallets[:5]:
            print(f'  {w.get("address", "")[:12]}... — {w.get("label", "unknown")}')
