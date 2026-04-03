"""
Safety filter chain orchestrator.
Runs all 16 safety filters in fast-fail order before any buy is executed.
Each filter returns a FilterResult. Hard rejects stop the chain immediately.
"""

import time
from datetime import datetime, timezone
from dataclasses import dataclass

from config.constants import (
    MIN_TOKEN_AGE_SECONDS, MIN_LIQUIDITY_USD, MIN_UNIQUE_BUYERS, MIN_UNIQUE_SELLERS,
    REQUIRE_VERIFIED_SOURCE, REJECT_PROXY_CONTRACTS,
)
from safety.honeypot import run_honeypot_checks
from safety.contract_analysis import run_contract_analysis
from safety.lp_lock import check_lp_lock, is_lp_burned
from safety.holder_analysis import check_holder_concentration
from monitoring.logger import write_log, insert_safety_check, get_active_tokens, update_token


@dataclass
class FilterResult:
    name: str
    passed: bool
    reason: str = ''
    severity: str = 'info'  # info, warning, hard_reject


def run_filter_chain(contract_address: str, dex_data: dict | None = None,
                     token_data: dict | None = None) -> dict:
    """
    Run all safety filters for a token in fast-fail order.

    Args:
        contract_address: The token contract address
        dex_data: DexScreener data for the token (if available)
        token_data: Database token record (if available)

    Returns:
        dict with keys:
            passed: bool — True if all hard filters pass
            results: list[FilterResult] — individual filter results
            penalties: list[tuple[str, int]] — soft scoring penalties
            goplus_data: dict — raw GoPlus data for reuse in scoring
            summary: str — human-readable summary
    """
    results: list[FilterResult] = []
    penalties: list[tuple[str, int]] = []
    goplus_data = {}

    def _add_result(name: str, passed: bool, reason: str = '', severity: str = 'info') -> bool:
        """Add a filter result and log it. Returns False if hard reject."""
        result = FilterResult(name=name, passed=passed, reason=reason, severity=severity)
        results.append(result)
        insert_safety_check(contract_address, name, passed, reason, severity)
        if not passed and severity == 'hard_reject':
            return False  # Stop chain
        return True  # Continue chain

    # === Filter 1: Contract verified on Basescan ===
    if REQUIRE_VERIFIED_SOURCE:
        from safety.contract_analysis import check_verified
        verification = check_verified(contract_address)
        if not _add_result(
            'contract_verified',
            verification.get('verified', False),
            'Contract source code not verified on Basescan' if not verification.get('verified') else '',
            'hard_reject' if not verification.get('verified') else 'info',
        ):
            return _build_response(False, results, penalties, goplus_data)

    # === Filter 2: Token age >= 20 minutes ===
    token_age_ok = True
    if dex_data and dex_data.get('pair_created_at'):
        created_ms = dex_data['pair_created_at']
        if isinstance(created_ms, (int, float)):
            age_seconds = time.time() - (created_ms / 1000)
            token_age_ok = age_seconds >= MIN_TOKEN_AGE_SECONDS
    elif token_data and token_data.get('first_seen'):
        try:
            seen_dt = datetime.fromisoformat(token_data['first_seen'])
            if seen_dt.tzinfo is None:
                seen_dt = seen_dt.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - seen_dt).total_seconds()
            token_age_ok = age_seconds >= MIN_TOKEN_AGE_SECONDS
        except (ValueError, TypeError):
            token_age_ok = False

    if not _add_result(
        'token_age',
        token_age_ok,
        f'Token must be trading for at least {MIN_TOKEN_AGE_SECONDS // 60} minutes' if not token_age_ok else '',
        'hard_reject' if not token_age_ok else 'info',
    ):
        return _build_response(False, results, penalties, goplus_data)

    # === Filter 3-8: GoPlus + Honeypot checks (one API call covers multiple filters) ===
    honeypot_result = run_honeypot_checks(contract_address)
    goplus_data = honeypot_result.get('goplus', {})

    # Filter 3: Honeypot check
    if not _add_result(
        'honeypot_check',
        not honeypot_result.get('is_honeypot', True),
        '; '.join(honeypot_result.get('reasons', [])) if honeypot_result.get('is_honeypot') else '',
        'hard_reject' if honeypot_result.get('is_honeypot') else 'info',
    ):
        return _build_response(False, results, penalties, goplus_data)

    # Filter 4: Buy/sell tax
    tax_ok = honeypot_result.get('passed', False) if not honeypot_result.get('is_honeypot') else False
    tax_reasons = [r for r in honeypot_result.get('reasons', []) if 'tax' in r.lower()]
    if not _add_result(
        'tax_check',
        len(tax_reasons) == 0,
        '; '.join(tax_reasons) if tax_reasons else '',
        'hard_reject' if tax_reasons else 'info',
    ):
        return _build_response(False, results, penalties, goplus_data)

    # === Filter 5-8: Contract analysis (uses GoPlus data) ===
    contract_result = run_contract_analysis(contract_address, goplus_data)

    # Filter 5: Not a proxy contract
    if REJECT_PROXY_CONTRACTS and contract_result.get('is_proxy'):
        if not _add_result('proxy_check', False, 'Upgradeable proxy contract', 'hard_reject'):
            return _build_response(False, results, penalties, goplus_data)
    else:
        _add_result('proxy_check', True)

    # Filter 6: No hidden mint function
    if contract_result.get('is_mintable'):
        if not _add_result('mint_check', False, 'Owner can mint unlimited tokens', 'hard_reject'):
            return _build_response(False, results, penalties, goplus_data)
    else:
        _add_result('mint_check', True)

    # Filter 7: No blacklist capability (if owner active)
    if not contract_result.get('passed'):
        blacklist_reasons = [r for r in contract_result.get('reasons', []) if 'blacklist' in r.lower()]
        if blacklist_reasons:
            if not _add_result('blacklist_check', False, '; '.join(blacklist_reasons), 'hard_reject'):
                return _build_response(False, results, penalties, goplus_data)

    # Filter 8: Can't take back ownership
    takeback_reasons = [r for r in contract_result.get('reasons', []) if 'take back' in r.lower()]
    if takeback_reasons:
        if not _add_result('ownership_takeback', False, '; '.join(takeback_reasons), 'hard_reject'):
            return _build_response(False, results, penalties, goplus_data)
    else:
        _add_result('ownership_takeback', True)

    # Check for other hard rejects from contract analysis
    other_hard_reasons = [r for r in contract_result.get('reasons', [])
                          if 'blacklist' not in r.lower() and 'take back' not in r.lower()
                          and 'verified' not in r.lower()]
    if other_hard_reasons:
        if not _add_result('contract_safety', False, '; '.join(other_hard_reasons), 'hard_reject'):
            return _build_response(False, results, penalties, goplus_data)
    else:
        _add_result('contract_safety', True)

    # Collect soft penalties from contract analysis
    penalties.extend(contract_result.get('penalties', []))

    # === Filter 9: Minimum liquidity ===
    liquidity = 0
    if dex_data:
        liquidity = dex_data.get('liquidity_usd', 0)
    elif token_data:
        liquidity = token_data.get('liquidity_usd', 0)

    if not _add_result(
        'min_liquidity',
        liquidity >= MIN_LIQUIDITY_USD,
        f'Liquidity ${liquidity:,.0f} < ${MIN_LIQUIDITY_USD:,.0f} minimum' if liquidity < MIN_LIQUIDITY_USD else '',
        'hard_reject' if liquidity < MIN_LIQUIDITY_USD else 'info',
    ):
        return _build_response(False, results, penalties, goplus_data)

    # === Filter 10: LP locked/burned ===
    lp_result = check_lp_lock(goplus_data)
    if not _add_result(
        'lp_lock',
        lp_result.get('passed', False),
        '; '.join(lp_result.get('reasons', [])) if not lp_result.get('passed') else '',
        'hard_reject' if not lp_result.get('passed') else 'info',
    ):
        return _build_response(False, results, penalties, goplus_data)

    # LP burned bonus for scoring
    if is_lp_burned(goplus_data):
        penalties.append(('lp_burned', 10))  # Positive penalty = bonus

    # === Filter 11: Wallet concentration ===
    pool_address = ''
    if dex_data:
        pool_address = dex_data.get('pool_address', '')
    elif token_data:
        pool_address = token_data.get('pool_address', '')

    holder_result = check_holder_concentration(goplus_data, pool_address)
    if not _add_result(
        'holder_concentration',
        holder_result.get('passed', False),
        '; '.join(holder_result.get('reasons', [])) if not holder_result.get('passed') else '',
        'hard_reject' if not holder_result.get('passed') else 'info',
    ):
        return _build_response(False, results, penalties, goplus_data)

    # === Filter 12: Minimum unique buyers/sellers ===
    if dex_data:
        h24_buys = dex_data.get('h24_buys', 0)
        h24_sells = dex_data.get('h24_sells', 0)
        buyers_ok = h24_buys >= MIN_UNIQUE_BUYERS
        sellers_ok = h24_sells >= MIN_UNIQUE_SELLERS

        if not buyers_ok or not sellers_ok:
            reason = []
            if not buyers_ok:
                reason.append(f'{h24_buys} buyers (need {MIN_UNIQUE_BUYERS})')
            if not sellers_ok:
                reason.append(f'{h24_sells} sellers (need {MIN_UNIQUE_SELLERS})')
            if not _add_result(
                'trading_activity',
                False,
                '; '.join(reason),
                'hard_reject',
            ):
                return _build_response(False, results, penalties, goplus_data)
        else:
            _add_result('trading_activity', True)
    else:
        _add_result('trading_activity', True, 'No DexScreener data — skipping activity check', 'warning')

    # === Filters 13-16: Soft penalties (already collected from contract_analysis) ===

    # Filter 13: Ownership not renounced (soft penalty — already in contract_result.penalties)
    if contract_result.get('ownership_renounced'):
        penalties.append(('ownership_renounced', 10))  # Bonus for renounced

    # Filter 14: External calls (already in contract_result.penalties)
    # Filter 15: Anti-whale modifiable (already in contract_result.penalties)
    # Filter 16: Serial deployer (already in contract_result.penalties)

    # Zero tax bonus
    buy_tax = honeypot_result.get('buy_tax', 0)
    sell_tax = honeypot_result.get('sell_tax', 0)
    if buy_tax == 0 and sell_tax == 0:
        penalties.append(('zero_tax', 5))
    elif buy_tax > 3 or sell_tax > 3:
        penalties.append(('high_tax', -15))

    # All hard filters passed
    return _build_response(True, results, penalties, goplus_data)


def _build_response(passed: bool, results: list[FilterResult],
                    penalties: list[tuple[str, int]], goplus_data: dict) -> dict:
    """Build the final response dict."""
    failed = [r for r in results if not r.passed]
    hard_rejects = [r for r in failed if r.severity == 'hard_reject']

    summary_parts = []
    if passed:
        summary_parts.append(f'PASSED all {len(results)} filters')
    else:
        summary_parts.append(f'FAILED — {len(hard_rejects)} hard reject(s)')
        for r in hard_rejects:
            summary_parts.append(f'  [{r.name}] {r.reason}')

    if penalties:
        penalty_total = sum(p[1] for p in penalties)
        summary_parts.append(f'  Scoring adjustments: {penalty_total:+d} points from {len(penalties)} signals')

    return {
        'passed': passed,
        'results': results,
        'penalties': penalties,
        'goplus_data': goplus_data,
        'summary': '\n'.join(summary_parts),
        'hard_reject_count': len(hard_rejects),
        'filter_count': len(results),
    }


def rescan_active_tokens() -> list[str]:
    """
    Re-scan active tokens through safety filters.
    Called periodically to catch tokens that become honeypots after launch.
    Returns list of contract addresses that were flagged.
    """
    tokens = get_active_tokens()
    flagged = []

    for token in tokens:
        address = token['contract_address']

        # Quick GoPlus check only (not full filter chain — too expensive)
        try:
            honeypot_result = run_honeypot_checks(address)
            if honeypot_result.get('is_honeypot'):
                update_token(address, status='flagged', is_honeypot=1)
                flagged.append(address)
                write_log(f'SAFETY RESCAN | {token.get("symbol", address[:12])} flagged as honeypot')

            # Update tax data
            update_token(address,
                         buy_tax=honeypot_result.get('buy_tax', 0),
                         sell_tax=honeypot_result.get('sell_tax', 0))

        except Exception as e:
            write_log(f'SAFETY RESCAN | Error checking {address[:12]}: {e}')

    return flagged


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('Usage: python -m safety.filter_chain <contract_address>')
        sys.exit(1)

    address = sys.argv[1]
    print(f'Running full safety filter chain for {address}...\n')

    result = run_filter_chain(address)

    print(result['summary'])
    print()

    for r in result['results']:
        status = 'PASS' if r.passed else 'FAIL'
        print(f'  [{status}] {r.name}: {r.reason or "OK"}')

    if result['penalties']:
        print()
        print('  Scoring penalties:')
        for name, points in result['penalties']:
            print(f'    {name}: {points:+d}')
