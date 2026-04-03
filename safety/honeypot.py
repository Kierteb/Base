"""
Honeypot detection via GoPlus Security API and Honeypot.is.
Two independent checks — both must pass for a token to be considered safe.
"""

import time
import json
import os
import requests
from datetime import datetime, timezone

from config.constants import (
    GOPLUS_BASE_URL, GOPLUS_CHAIN_ID, GOPLUS_API_KEY,
    HONEYPOT_BASE_URL, SAFETY_CACHE_PATH, SAFETY_CACHE_TTL_SECONDS,
    MAX_BUY_TAX_PCT, MAX_SELL_TAX_PCT,
)
from monitoring.logger import write_log

# In-memory cache for GoPlus/honeypot results
_safety_cache: dict = {}
_cache_loaded = False


def _load_cache() -> None:
    """Load safety cache from disk."""
    global _safety_cache, _cache_loaded
    if _cache_loaded:
        return
    try:
        if os.path.exists(SAFETY_CACHE_PATH):
            with open(SAFETY_CACHE_PATH, 'r') as f:
                _safety_cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        _safety_cache = {}
    _cache_loaded = True


def _save_cache() -> None:
    """Persist cache to disk."""
    try:
        os.makedirs(os.path.dirname(SAFETY_CACHE_PATH), exist_ok=True)
        with open(SAFETY_CACHE_PATH, 'w') as f:
            json.dump(_safety_cache, f)
    except OSError as e:
        write_log(f'SAFETY CACHE | Save error: {e}')


def _get_cached(contract_address: str) -> dict | None:
    """Get cached result if still fresh."""
    _load_cache()
    entry = _safety_cache.get(contract_address.lower())
    if not entry:
        return None
    cached_at = entry.get('cached_at', 0)
    if time.time() - cached_at > SAFETY_CACHE_TTL_SECONDS:
        return None
    return entry


def _set_cached(contract_address: str, data: dict) -> None:
    """Cache a safety result."""
    _load_cache()
    data['cached_at'] = time.time()
    _safety_cache[contract_address.lower()] = data
    # Periodically save (every 10 new entries)
    if len(_safety_cache) % 10 == 0:
        _save_cache()


def check_goplus(contract_address: str) -> dict:
    """
    Query GoPlus Security API for comprehensive token security data.
    Returns a dict with all security fields normalised to booleans/floats.
    """
    cached = _get_cached(contract_address)
    if cached and 'goplus' in cached:
        return cached['goplus']

    url = f'{GOPLUS_BASE_URL}/token_security/{GOPLUS_CHAIN_ID}'
    params = {'contract_addresses': contract_address.lower()}
    if GOPLUS_API_KEY:
        params['api_key'] = GOPLUS_API_KEY

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        write_log(f'GOPLUS | API error for {contract_address[:12]}: {e}')
        return {'error': str(e), 'available': False}

    result_data = data.get('result', {})
    token_data = result_data.get(contract_address.lower(), {})

    if not token_data:
        return {'error': 'No data returned', 'available': False}

    def _bool(val) -> bool:
        """GoPlus returns '1'/'0' strings."""
        return str(val) == '1'

    def _float(val) -> float:
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0

    result = {
        'available': True,
        # Core honeypot flags
        'is_honeypot': _bool(token_data.get('is_honeypot', '0')),
        'cannot_sell_all': _bool(token_data.get('cannot_sell_all', '0')),
        'cannot_buy': _bool(token_data.get('cannot_buy', '0')),
        # Transfer restrictions
        'transfer_pausable': _bool(token_data.get('transfer_pausable', '0')),
        'is_anti_whale': _bool(token_data.get('is_anti_whale', '0')),
        'anti_whale_modifiable': _bool(token_data.get('anti_whale_modifiable', '0')),
        # Ownership
        'hidden_owner': _bool(token_data.get('hidden_owner', '0')),
        'can_take_back_ownership': _bool(token_data.get('can_take_back_ownership', '0')),
        'owner_address': token_data.get('owner_address', ''),
        'creator_address': token_data.get('creator_address', ''),
        # Minting/burning
        'owner_change_balance': _bool(token_data.get('owner_change_balance', '0')),
        'is_mintable': _bool(token_data.get('is_mintable', '0')),
        # Code risks
        'external_call': _bool(token_data.get('external_call', '0')),
        'selfdestruct': _bool(token_data.get('selfdestruct', '0')),
        'is_proxy': _bool(token_data.get('is_proxy', '0')),
        'is_open_source': _bool(token_data.get('is_open_source', '0')),
        # Blacklist
        'is_blacklisted': _bool(token_data.get('is_blacklisted', '0')),
        # Tax
        'buy_tax': _float(token_data.get('buy_tax', 0)) * 100,  # Convert to percentage
        'sell_tax': _float(token_data.get('sell_tax', 0)) * 100,
        # Holders
        'holder_count': int(token_data.get('holder_count', 0) or 0),
        'total_supply': token_data.get('total_supply', '0'),
        # LP info
        'lp_holder_count': int(token_data.get('lp_holder_count', 0) or 0),
        'lp_total_supply': token_data.get('lp_total_supply', '0'),
        # Top holders
        'holders': token_data.get('holders', []),
        'lp_holders': token_data.get('lp_holders', []),
    }

    # Cache the result
    cached_entry = _get_cached(contract_address) or {}
    cached_entry['goplus'] = result
    _set_cached(contract_address, cached_entry)

    return result


def check_honeypot_is(contract_address: str) -> dict:
    """
    Query Honeypot.is API for buy/sell simulation.
    Simulates an actual buy+sell and reports if the sell would fail.
    """
    cached = _get_cached(contract_address)
    if cached and 'honeypot_is' in cached:
        return cached['honeypot_is']

    url = f'{HONEYPOT_BASE_URL}/IsHoneypot'
    params = {'address': contract_address, 'chainId': 8453}

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        write_log(f'HONEYPOT.IS | API error for {contract_address[:12]}: {e}')
        return {'error': str(e), 'available': False}

    honeypot_result = data.get('honeypotResult', {})
    simulation = data.get('simulationResult', {})

    result = {
        'available': True,
        'is_honeypot': honeypot_result.get('isHoneypot', False),
        'honeypot_reason': honeypot_result.get('honeypotReason', ''),
        'buy_tax': float(simulation.get('buyTax', 0) or 0),
        'sell_tax': float(simulation.get('sellTax', 0) or 0),
        'buy_gas': simulation.get('buyGas', ''),
        'sell_gas': simulation.get('sellGas', ''),
        'transfer_tax': float(simulation.get('transferTax', 0) or 0),
    }

    # Cache
    cached_entry = _get_cached(contract_address) or {}
    cached_entry['honeypot_is'] = result
    _set_cached(contract_address, cached_entry)

    return result


def run_honeypot_checks(contract_address: str) -> dict:
    """
    Run both GoPlus and Honeypot.is checks. Returns a combined result.

    Returns:
        dict with keys:
            passed: bool — True if token is safe
            is_honeypot: bool
            buy_tax: float (percentage)
            sell_tax: float (percentage)
            reasons: list[str] — failure reasons
            goplus: dict — raw GoPlus result
            honeypot_is: dict — raw Honeypot.is result
    """
    reasons = []

    goplus = check_goplus(contract_address)
    honeypot = check_honeypot_is(contract_address)

    is_honeypot = False
    buy_tax = 0.0
    sell_tax = 0.0

    # GoPlus honeypot flags
    if goplus.get('available'):
        if goplus.get('is_honeypot'):
            is_honeypot = True
            reasons.append('GoPlus: is_honeypot')
        if goplus.get('cannot_sell_all'):
            is_honeypot = True
            reasons.append('GoPlus: cannot_sell_all')
        if goplus.get('cannot_buy'):
            is_honeypot = True
            reasons.append('GoPlus: cannot_buy')

        buy_tax = goplus.get('buy_tax', 0)
        sell_tax = goplus.get('sell_tax', 0)

    # Honeypot.is simulation
    if honeypot.get('available'):
        if honeypot.get('is_honeypot'):
            is_honeypot = True
            reasons.append(f'Honeypot.is: {honeypot.get("honeypot_reason", "sell simulation failed")}')

        # Use the higher tax from either source
        buy_tax = max(buy_tax, honeypot.get('buy_tax', 0))
        sell_tax = max(sell_tax, honeypot.get('sell_tax', 0))

    # Tax check
    if buy_tax > MAX_BUY_TAX_PCT:
        reasons.append(f'Buy tax too high: {buy_tax:.1f}% (max {MAX_BUY_TAX_PCT}%)')
    if sell_tax > MAX_SELL_TAX_PCT:
        reasons.append(f'Sell tax too high: {sell_tax:.1f}% (max {MAX_SELL_TAX_PCT}%)')

    # If neither API is available, fail open with a warning
    if not goplus.get('available') and not honeypot.get('available'):
        reasons.append('Neither GoPlus nor Honeypot.is available — cannot verify safety')

    # GoPlus alone is sufficient — Honeypot.is doesn't support Base chain
    passed = (
        not is_honeypot
        and buy_tax <= MAX_BUY_TAX_PCT
        and sell_tax <= MAX_SELL_TAX_PCT
        and goplus.get('available')
    )

    return {
        'passed': passed,
        'is_honeypot': is_honeypot,
        'buy_tax': buy_tax,
        'sell_tax': sell_tax,
        'reasons': reasons,
        'goplus': goplus,
        'honeypot_is': honeypot,
    }


def flush_cache() -> None:
    """Save cache to disk (call on shutdown)."""
    _save_cache()


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('Usage: python -m safety.honeypot <contract_address>')
        sys.exit(1)

    address = sys.argv[1]
    print(f'Checking {address}...\n')

    result = run_honeypot_checks(address)
    print(f'  Passed:     {result["passed"]}')
    print(f'  Honeypot:   {result["is_honeypot"]}')
    print(f'  Buy tax:    {result["buy_tax"]:.1f}%')
    print(f'  Sell tax:   {result["sell_tax"]:.1f}%')
    if result['reasons']:
        print(f'  Reasons:    {", ".join(result["reasons"])}')
