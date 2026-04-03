"""
Market regime filter based on ETH price.
Blocks new buys when ETH is dumping (macro risk).
"""

import requests
from datetime import datetime, timezone

from config.constants import ETH_DUMP_6H_THRESHOLD, ETH_DUMP_24H_THRESHOLD
from monitoring.logger import write_log

# Global regime state
_regime = {
    'block_new_buys': False,
    'block_all_trades': False,
    'eth_h6_change': 0.0,
    'eth_h24_change': 0.0,
    'last_updated': None,
}


def get_regime() -> dict:
    """Get current market regime state."""
    return _regime.copy()


def is_trading_blocked() -> bool:
    """Check if all trading is blocked (ETH dumping hard)."""
    return _regime.get('block_all_trades', False)


def is_buying_blocked() -> bool:
    """Check if new buys are blocked (ETH dumping moderately)."""
    return _regime.get('block_new_buys', False) or _regime.get('block_all_trades', False)


def update_regime() -> None:
    """
    Fetch ETH price data and update regime state.
    Uses DexScreener ETH/USDC pair on Base.
    """
    global _regime

    try:
        # Fetch ETH price data from DexScreener (WETH/USDC on Base)
        resp = requests.get(
            'https://api.dexscreener.com/latest/dex/tokens/0x4200000000000000000000000000000000000006',
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        write_log(f'REGIME | ETH price fetch error: {e}')
        return

    pairs = data.get('pairs') or []

    # Find the most liquid Base ETH pair
    best_pair = None
    best_liquidity = 0
    for pair in pairs:
        if pair.get('chainId') != 'base':
            continue
        liq = float((pair.get('liquidity') or {}).get('usd', 0) or 0)
        if liq > best_liquidity:
            best_liquidity = liq
            best_pair = pair

    if not best_pair:
        return

    price_change = best_pair.get('priceChange') or {}
    h6_change = float(price_change.get('h6', 0) or 0)
    h24_change = float(price_change.get('h24', 0) or 0)

    block_new_buys = h6_change <= ETH_DUMP_6H_THRESHOLD
    block_all = h24_change <= ETH_DUMP_24H_THRESHOLD

    prev_buy_blocked = _regime.get('block_new_buys', False)
    prev_all_blocked = _regime.get('block_all_trades', False)

    _regime = {
        'block_new_buys': block_new_buys,
        'block_all_trades': block_all,
        'eth_h6_change': h6_change,
        'eth_h24_change': h24_change,
        'last_updated': datetime.now(timezone.utc).isoformat(),
    }

    # Log regime changes
    if block_all and not prev_all_blocked:
        write_log(f'REGIME | ALL TRADING BLOCKED — ETH 24h: {h24_change:+.1f}% (threshold: {ETH_DUMP_24H_THRESHOLD}%)')
    elif block_new_buys and not prev_buy_blocked:
        write_log(f'REGIME | NEW BUYS BLOCKED — ETH 6h: {h6_change:+.1f}% (threshold: {ETH_DUMP_6H_THRESHOLD}%)')
    elif not block_new_buys and prev_buy_blocked:
        write_log(f'REGIME | Trading resumed — ETH 6h: {h6_change:+.1f}%')
