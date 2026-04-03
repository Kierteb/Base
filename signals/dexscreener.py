"""
DexScreener REST API integration for Base chain.
Batch-fetches token data: volume, liquidity, price, buy/sell ratio, market cap.
Also provides Base chain pair search and trending detection.
"""

import requests
import time

from config.constants import DEXSCREENER_BASE_URL, DEXSCREENER_BATCH_SIZE
from monitoring.logger import write_log


def fetch_token_data(contract_addresses: list[str]) -> dict[str, dict]:
    """
    Fetch DexScreener data for a list of token contract addresses.
    Batches into groups of 30 (API limit). Returns dict keyed by contract address.
    """
    results = {}

    for i in range(0, len(contract_addresses), DEXSCREENER_BATCH_SIZE):
        batch = contract_addresses[i:i + DEXSCREENER_BATCH_SIZE]
        batch_str = ','.join(batch)

        try:
            resp = requests.get(f'{DEXSCREENER_BASE_URL}/tokens/{batch_str}', timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            write_log(f'DEXSCREENER | Batch fetch error: {e}')
            continue

        pairs = data.get('pairs') or []
        for pair in pairs:
            # Only include Base chain pairs
            if pair.get('chainId') != 'base':
                continue

            address = pair.get('baseToken', {}).get('address', '').lower()
            if not address or address in results:
                continue

            volume = pair.get('volume') or {}
            price_change = pair.get('priceChange') or {}
            txns = pair.get('txns') or {}
            h1_txns = txns.get('h1') or {}
            h24_txns = txns.get('h24') or {}
            liquidity = pair.get('liquidity') or {}

            h1_buys = int(h1_txns.get('buys', 0) or 0)
            h1_sells = int(h1_txns.get('sells', 0) or 0)

            results[address] = {
                'symbol': pair.get('baseToken', {}).get('symbol', ''),
                'name': pair.get('baseToken', {}).get('name', ''),
                'price_usd': float(pair.get('priceUsd') or 0),
                'price_native': float(pair.get('priceNative') or 0),
                'h1_volume': float(volume.get('h1', 0) or 0),
                'h6_volume': float(volume.get('h6', 0) or 0),
                'h24_volume': float(volume.get('h24', 0) or 0),
                'm5_price_change': float(price_change.get('m5', 0) or 0),
                'h1_price_change': float(price_change.get('h1', 0) or 0),
                'h6_price_change': float(price_change.get('h6', 0) or 0),
                'h24_price_change': float(price_change.get('h24', 0) or 0),
                'h1_buys': h1_buys,
                'h1_sells': h1_sells,
                'h24_buys': int(h24_txns.get('buys', 0) or 0),
                'h24_sells': int(h24_txns.get('sells', 0) or 0),
                'buy_sell_ratio': min(h1_buys / h1_sells, 50) if h1_sells > 0 else min(h1_buys, 50),
                'liquidity_usd': float(liquidity.get('usd', 0) or 0),
                'market_cap': float(pair.get('marketCap') or pair.get('fdv') or 0),
                'pair_address': pair.get('pairAddress', ''),
                'pool_address': pair.get('pairAddress', ''),
                'dex_id': pair.get('dexId', ''),
                'pair_created_at': pair.get('pairCreatedAt'),
                'url': pair.get('url', ''),
            }

        if i + DEXSCREENER_BATCH_SIZE < len(contract_addresses):
            time.sleep(0.5)

    return results


def fetch_single_token(contract_address: str) -> dict | None:
    """Fetch DexScreener data for a single token. Returns None if not found."""
    results = fetch_token_data([contract_address])
    return results.get(contract_address.lower())


def fetch_base_new_pairs(limit: int = 50) -> list[dict]:
    """
    Fetch Base chain tokens from multiple DexScreener endpoints:
    1. Token boosts (promoted tokens)
    2. Search queries for Base trending categories
    3. Token profiles
    """
    all_pairs = {}

    # Method 1: Token profiles (latest promoted)
    try:
        resp = requests.get('https://api.dexscreener.com/token-profiles/latest/v1', timeout=10)
        resp.raise_for_status()
        profiles = resp.json()
        base_addresses = [p['tokenAddress'] for p in profiles if p.get('chainId') == 'base']
        if base_addresses:
            token_data = fetch_token_data(base_addresses)
            for addr, data in token_data.items():
                all_pairs[addr] = data
    except (requests.RequestException, ValueError) as e:
        write_log(f'DEXSCREENER | Token profiles error: {e}')

    # Method 2: Token boosts
    try:
        boosts = fetch_token_boosts()
        if boosts:
            boost_addresses = [b['contract_address'] for b in boosts if b.get('contract_address')]
            if boost_addresses:
                token_data = fetch_token_data(boost_addresses)
                for addr, data in token_data.items():
                    all_pairs[addr] = data
    except Exception as e:
        write_log(f'DEXSCREENER | Token boosts error: {e}')

    # Method 3: Search queries for trending Base tokens
    search_queries = ['base meme', 'base new', 'base trending', 'base degen']
    for query in search_queries:
        try:
            resp = requests.get(
                f'{DEXSCREENER_BASE_URL}/search',
                params={'q': query},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            for pair in (data.get('pairs') or []):
                if pair.get('chainId') != 'base':
                    continue
                address = pair.get('baseToken', {}).get('address', '').lower()
                if not address or address in all_pairs:
                    continue

                liquidity = pair.get('liquidity') or {}
                volume = pair.get('volume') or {}

                all_pairs[address] = {
                    'contract_address': address,
                    'symbol': pair.get('baseToken', {}).get('symbol', ''),
                    'name': pair.get('baseToken', {}).get('name', ''),
                    'price_usd': float(pair.get('priceUsd') or 0),
                    'liquidity_usd': float(liquidity.get('usd', 0) or 0),
                    'h24_volume': float(volume.get('h24', 0) or 0),
                    'pair_address': pair.get('pairAddress', ''),
                    'dex_id': pair.get('dexId', ''),
                    'pair_created_at': pair.get('pairCreatedAt'),
                    'url': pair.get('url', ''),
                }
            time.sleep(0.3)
        except (requests.RequestException, ValueError) as e:
            write_log(f'DEXSCREENER | Search "{query}" error: {e}')

    results = list(all_pairs.values())[:limit]
    if results:
        write_log(f'DEXSCREENER | Discovered {len(results)} Base token(s) from profiles/boosts/search')
    return results


def search_base_tokens(query: str) -> list[dict]:
    """Search DexScreener for Base chain tokens matching a query."""
    try:
        resp = requests.get(
            f'{DEXSCREENER_BASE_URL}/search',
            params={'q': query},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        write_log(f'DEXSCREENER | Search error: {e}')
        return []

    pairs = data.get('pairs') or []
    return [
        {
            'contract_address': p.get('baseToken', {}).get('address', '').lower(),
            'symbol': p.get('baseToken', {}).get('symbol', ''),
            'name': p.get('baseToken', {}).get('name', ''),
            'price_usd': float(p.get('priceUsd') or 0),
            'liquidity_usd': float((p.get('liquidity') or {}).get('usd', 0) or 0),
            'dex_id': p.get('dexId', ''),
            'pair_created_at': p.get('pairCreatedAt'),
        }
        for p in pairs
        if p.get('chainId') == 'base'
    ]


def fetch_token_boosts() -> list[dict]:
    """Fetch currently boosted tokens from DexScreener (promoted tokens)."""
    try:
        resp = requests.get('https://api.dexscreener.com/token-boosts/latest/v1', timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        write_log(f'DEXSCREENER | Token boosts fetch error: {e}')
        return []

    return [
        {
            'contract_address': t.get('tokenAddress', '').lower(),
            'chain_id': t.get('chainId', ''),
            'amount': t.get('amount', 0),
        }
        for t in data
        if t.get('chainId') == 'base'
    ]


if __name__ == '__main__':
    print('Fetching Base chain new pairs...\n')
    pairs = fetch_base_new_pairs(limit=5)
    for p in pairs:
        print(f'  {p["symbol"]:>10} | ${p["price_usd"]:.8f} | Liq: ${p["liquidity_usd"]:,.0f} | Vol: ${p["h24_volume"]:,.0f} | {p["dex_id"]}')

    print(f'\n  Found {len(pairs)} pairs')
