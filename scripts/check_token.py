#!/usr/bin/env python3
"""
Manual token safety check.
Usage: python scripts/check_token.py <contract_address>

Runs the full safety filter chain and scoring engine on a token,
printing detailed results.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from safety.filter_chain import run_filter_chain
from signals.dexscreener import fetch_single_token
from scoring.engine import score_token


def check_token(address: str) -> None:
    print(f'\n  Checking token: {address}\n')
    print(f'  {"=" * 60}')

    # 1. Fetch DexScreener data
    print(f'\n  [1/3] Fetching DexScreener data...')
    dex = fetch_single_token(address)
    if dex:
        print(f'    Symbol:     {dex.get("symbol", "?")}')
        print(f'    Price:      ${dex.get("price_usd", 0):.8f}')
        print(f'    Liquidity:  ${dex.get("liquidity_usd", 0):,.0f}')
        print(f'    MCap:       ${dex.get("market_cap", 0):,.0f}')
        print(f'    1h Change:  {dex.get("h1_price_change", 0):+.1f}%')
        print(f'    1h Volume:  ${dex.get("h1_volume", 0):,.0f}')
        print(f'    24h Volume: ${dex.get("h24_volume", 0):,.0f}')
        print(f'    Buy/Sell:   {dex.get("h1_buys", 0)}/{dex.get("h1_sells", 0)} (ratio: {dex.get("buy_sell_ratio", 0):.1f})')
        print(f'    DEX:        {dex.get("dex_id", "?")}')
    else:
        print(f'    No DexScreener data found.')

    # 2. Run safety filter chain
    print(f'\n  [2/3] Running safety filter chain...')
    safety = run_filter_chain(address, dex_data=dex)

    print(f'\n    {safety["summary"]}')
    print()

    for r in safety['results']:
        status = '  PASS' if r.passed else '  FAIL'
        severity = f' [{r.severity}]' if not r.passed else ''
        reason = f' — {r.reason}' if r.reason else ''
        print(f'    [{status}]{severity} {r.name}{reason}')

    if safety['penalties']:
        print(f'\n    Scoring adjustments:')
        for name, pts in safety['penalties']:
            print(f'      {name}: {pts:+d}')

    # 3. Score the token
    print(f'\n  [3/3] Running scoring engine...')
    token_data = {
        'contract_address': address,
        'symbol': dex.get('symbol', '?') if dex else '?',
    }

    result = score_token(token_data, dex, safety_penalties=safety.get('penalties'))
    print(f'\n    Score: {result["score"]}/100 (threshold: {result["threshold"]})')
    print(f'    Signal types: {result["signal_types_count"]} ({", ".join(result["signal_types"])})')
    print(f'    Meets threshold: {result["meets_threshold"]}')
    print(f'    Would alert: {result["should_alert"]}')

    if result['breakdown']:
        print(f'\n    Breakdown:')
        for item in result['breakdown']:
            pts = item.get('points', 0)
            prefix = '+' if pts > 0 else ''
            print(f'      {prefix}{pts:>3} — {item.get("signal", "")}')

    # Final verdict
    print(f'\n  {"=" * 60}')
    if safety['passed'] and result['meets_threshold']:
        print(f'  VERDICT: BUY SIGNAL — Safety passed, score {result["score"]} meets threshold')
    elif safety['passed']:
        print(f'  VERDICT: SAFE but below score threshold ({result["score"]} < {result["threshold"]})')
    else:
        print(f'  VERDICT: REJECTED — {safety["hard_reject_count"]} safety filter(s) failed')
    print()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python scripts/check_token.py <contract_address>')
        sys.exit(1)

    check_token(sys.argv[1])
