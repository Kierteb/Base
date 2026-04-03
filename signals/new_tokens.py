"""
New token discovery on Base chain.
Monitors DexScreener for new Base pairs and registers them for tracking.
"""

from datetime import datetime, timezone

from signals.dexscreener import fetch_base_new_pairs
from monitoring.logger import token_exists, insert_token, write_log


def discover_new_tokens() -> list[dict]:
    """
    Discover new tokens on Base chain via DexScreener new pairs.
    Registers them in the database with 'new' status.
    Returns list of newly discovered tokens.
    """
    pairs = fetch_base_new_pairs(limit=50)
    if not pairs:
        return []

    new_tokens = []
    for pair in pairs:
        address = pair.get('contract_address', '')
        if not address:
            continue

        if token_exists(address):
            continue

        insert_token(
            contract_address=address,
            symbol=pair.get('symbol', ''),
            name=pair.get('name', ''),
            deployer='',
            liquidity_usd=pair.get('liquidity_usd', 0),
            dex_id=pair.get('dex_id', ''),
            pool_address=pair.get('pair_address', ''),
            pair_address=pair.get('pair_address', ''),
        )
        new_tokens.append(pair)

    return new_tokens
