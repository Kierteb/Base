"""
Smart wallet discovery on Base chain.
Finds wallets that bought tokens early that later pumped.
Runs every 4 hours.
"""

import json
import requests
from datetime import datetime, timezone

from config.constants import (
    BASESCAN_API_KEY, BASESCAN_BASE_URL, SMART_WALLETS_PATH,
    MIN_WALLET_WIN_RATE, MIN_WALLET_PICKS,
)
from signals.dexscreener import fetch_base_new_pairs, fetch_single_token
from signals.wallets import load_smart_wallets, save_smart_wallets
from monitoring.logger import (
    insert_discovery_candidate, update_discovery_candidate,
    write_log,
)


def find_early_buyers(contract_address: str, limit: int = 50) -> list[str]:
    """
    Find early buyer addresses for a token via Basescan transfer events.
    Returns list of wallet addresses that bought early.
    """
    try:
        resp = requests.get(BASESCAN_BASE_URL, params={
            'module': 'account',
            'action': 'tokentx',
            'contractaddress': contract_address,
            'page': 1,
            'offset': limit,
            'sort': 'asc',  # Earliest first
            'apikey': BASESCAN_API_KEY,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        write_log(f'DISCOVERY | Basescan tokentx error: {e}')
        return []

    txns = data.get('result') or []
    if not isinstance(txns, list):
        return []

    # Buyers are 'to' addresses in early transfers (excluding the deployer/pool)
    buyers = set()
    for tx in txns:
        to_addr = (tx.get('to', '') or '').lower()
        from_addr = (tx.get('from', '') or '').lower()
        if to_addr and to_addr != from_addr:
            buyers.add(to_addr)

    return list(buyers)[:limit]


def run_discovery_cycle() -> None:
    """
    Discover new profitable wallets by finding early buyers of trending tokens.
    1. Get trending Base pairs from DexScreener
    2. Filter for >100% gainers in 4h
    3. Find early buyer addresses
    4. Track as candidates
    """
    try:
        # Get trending Base pairs
        pairs = fetch_base_new_pairs(limit=50)
        if not pairs:
            write_log('DISCOVERY | No new pairs found')
            return

        # Find strong performers — tokens that pumped significantly
        candidates_found = 0
        for pair in pairs:
            address = pair.get('contract_address', '')
            if not address:
                continue

            # Get detailed data
            dex = fetch_single_token(address)
            if not dex:
                continue

            # Only look at tokens that gained significantly
            h24_change = dex.get('h24_price_change', 0)
            if h24_change < 100:  # Need at least 100% gain
                continue

            # Find early buyers
            early_buyers = find_early_buyers(address, limit=30)
            if not early_buyers:
                continue

            # Track as discovery candidates
            existing_wallets = {w.get('address', '').lower() for w in load_smart_wallets()}
            for buyer in early_buyers:
                if buyer.lower() not in existing_wallets:
                    insert_discovery_candidate(buyer, source=f'early_buyer_{address[:12]}')
                    candidates_found += 1

        if candidates_found > 0:
            write_log(f'DISCOVERY | Found {candidates_found} new candidate wallet(s)')

    except Exception as e:
        write_log(f'DISCOVERY | Cycle error: {e}')
        import traceback
        traceback.print_exc()
