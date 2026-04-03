"""
Token holder concentration analysis.
Checks if too much supply is held by too few wallets (whale risk / rug risk).
"""

from config.constants import MAX_TOP3_WALLET_PCT, MIN_HOLDER_COUNT, BURN_ADDRESSES
from monitoring.logger import write_log


# Known addresses to exclude from concentration checks
EXCLUDED_ADDRESSES = BURN_ADDRESSES | {
    # Uniswap V3 related
    '0x2626664c2603336e57b271c5c0b26f421741e481',  # SwapRouter02
    '0x33128a8fc17869897dce68ed026d694621f6fdfd',  # V3 Factory
    # Aerodrome related
    '0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43',  # Router
    '0x420dd381b31aef6683db6b902084cb0ffece40da',  # Factory
}


def check_holder_concentration(goplus_data: dict, pool_address: str = '') -> dict:
    """
    Check if >20% of token supply is held by <3 wallets.
    Excludes pool contracts, burn addresses, and known DEX contracts.

    Args:
        goplus_data: GoPlus security data containing 'holders' list
        pool_address: The DEX pool address to exclude from concentration check

    Returns:
        dict with keys:
            passed: bool
            top3_pct: float
            holder_count: int
            top_holders: list[dict]
            reasons: list[str]
    """
    holders = goplus_data.get('holders', [])
    holder_count = int(goplus_data.get('holder_count', 0) or 0)

    if not holders:
        return {
            'passed': False,
            'top3_pct': 0.0,
            'holder_count': holder_count,
            'top_holders': [],
            'reasons': ['No holder data available from GoPlus'],
        }

    # Build exclusion set (add pool address if provided)
    exclusions = EXCLUDED_ADDRESSES.copy()
    if pool_address:
        exclusions.add(pool_address.lower())

    # Filter out excluded addresses and sort by percentage
    filtered_holders = []
    for h in holders:
        address = (h.get('address', '') or '').lower()
        if address in exclusions:
            continue
        pct = float(h.get('percent', 0) or 0) * 100  # GoPlus returns as decimal
        is_locked = bool(h.get('is_locked', False))
        is_contract = bool(h.get('is_contract', False))

        filtered_holders.append({
            'address': address,
            'pct': pct,
            'is_locked': is_locked,
            'is_contract': is_contract,
        })

    filtered_holders.sort(key=lambda x: x['pct'], reverse=True)

    # Calculate top 3 wallet concentration
    top3 = filtered_holders[:3]
    top3_pct = sum(h['pct'] for h in top3)

    reasons = []

    if top3_pct > MAX_TOP3_WALLET_PCT:
        top3_summary = ', '.join(f'{h["address"][:8]}...({h["pct"]:.1f}%)' for h in top3)
        reasons.append(f'Top 3 wallets hold {top3_pct:.1f}% (max {MAX_TOP3_WALLET_PCT}%): {top3_summary}')

    if holder_count < MIN_HOLDER_COUNT:
        reasons.append(f'Only {holder_count} holders (min {MIN_HOLDER_COUNT})')

    passed = top3_pct <= MAX_TOP3_WALLET_PCT

    return {
        'passed': passed,
        'top3_pct': round(top3_pct, 1),
        'holder_count': holder_count,
        'top_holders': top3,
        'reasons': reasons,
    }
