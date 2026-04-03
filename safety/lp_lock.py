"""
Liquidity pool lock verification.
Checks if LP tokens are locked via Team.Finance, Unicrypt, or burned.
"""

import requests

from config.constants import BURN_ADDRESSES
from monitoring.logger import write_log

# Known LP lock contracts on Base
KNOWN_LOCK_CONTRACTS = {
    # Team.Finance locker on Base
    '0xe2fe530c047f2d85298b07d9333c05d3e0c83ab2',
    # Unicrypt on Base
    '0x663a5c229c09b049e36dcc11a9b0d4a8eb9db214',
    # UNCX Network
    '0xdba68f07d1b7ca219f78ae8582c213d975c25caf',
}


def check_lp_lock(goplus_data: dict) -> dict:
    """
    Check if liquidity pool tokens are locked or burned.
    Uses GoPlus lp_holders data to determine lock percentage.

    Returns:
        dict with keys:
            passed: bool
            lp_locked_pct: float — percentage of LP locked/burned
            lock_details: list[dict] — individual lock entries
            reasons: list[str]
    """
    lp_holders = goplus_data.get('lp_holders', [])
    if not lp_holders:
        return {
            'passed': False,
            'lp_locked_pct': 0.0,
            'lock_details': [],
            'reasons': ['No LP holder data available from GoPlus'],
        }

    total_locked_pct = 0.0
    total_burned_pct = 0.0
    lock_details = []

    for holder in lp_holders:
        address = (holder.get('address', '') or '').lower()
        pct = float(holder.get('percent', 0) or 0) * 100  # GoPlus returns as decimal
        is_locked = bool(holder.get('is_locked', False))
        is_contract = bool(holder.get('is_contract', False))

        # Check if this is a burn address
        if address in BURN_ADDRESSES:
            total_burned_pct += pct
            lock_details.append({
                'address': address,
                'pct': pct,
                'type': 'burned',
            })
            continue

        # Check if this is a known lock contract
        if address in KNOWN_LOCK_CONTRACTS or is_locked:
            total_locked_pct += pct
            lock_details.append({
                'address': address,
                'pct': pct,
                'type': 'locked',
            })
            continue

        # If it's a contract and holds significant LP, it might be a lock we don't recognise
        if is_contract and pct > 5:
            # Could be a custom lock — count as partially locked with a note
            total_locked_pct += pct * 0.5  # Half credit for unknown contracts
            lock_details.append({
                'address': address,
                'pct': pct,
                'type': 'unknown_contract',
            })

    total_safe_pct = total_locked_pct + total_burned_pct

    from config.constants import MIN_LP_LOCKED_PCT
    passed = total_safe_pct >= MIN_LP_LOCKED_PCT

    reasons = []
    if not passed:
        reasons.append(f'LP locked/burned: {total_safe_pct:.1f}% (need {MIN_LP_LOCKED_PCT}%)')

    return {
        'passed': passed,
        'lp_locked_pct': round(total_safe_pct, 1),
        'lp_burned_pct': round(total_burned_pct, 1),
        'lp_locked_only_pct': round(total_locked_pct, 1),
        'lock_details': lock_details,
        'reasons': reasons,
    }


def is_lp_burned(goplus_data: dict) -> bool:
    """Quick check if majority of LP is burned (not just locked). Used for scoring bonus."""
    result = check_lp_lock(goplus_data)
    return result.get('lp_burned_pct', 0) >= 50
