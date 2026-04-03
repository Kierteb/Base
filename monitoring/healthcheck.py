"""
Component health monitoring.
Checks RPC, APIs, and scheduler status.
"""

import requests
import threading
import time

from config.constants import BASE_RPC_URL, BASESCAN_BASE_URL, BASESCAN_API_KEY
from monitoring.logger import write_log


def check_health() -> dict:
    """Run health checks on all components."""
    results = {}

    # Base RPC
    try:
        resp = requests.post(BASE_RPC_URL, json={
            'jsonrpc': '2.0', 'method': 'eth_blockNumber', 'params': [], 'id': 1,
        }, timeout=10)
        results['base_rpc'] = resp.ok
    except Exception:
        results['base_rpc'] = False

    # Basescan API
    try:
        resp = requests.get(BASESCAN_BASE_URL, params={
            'module': 'proxy', 'action': 'eth_blockNumber', 'apikey': BASESCAN_API_KEY,
        }, timeout=10)
        results['basescan'] = resp.ok
    except Exception:
        results['basescan'] = False

    # DexScreener
    try:
        resp = requests.get('https://api.dexscreener.com/latest/dex/pairs/base', timeout=10)
        results['dexscreener'] = resp.ok
    except Exception:
        results['dexscreener'] = False

    # GoPlus
    try:
        resp = requests.get('https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses=0x4200000000000000000000000000000000000006', timeout=10)
        results['goplus'] = resp.ok
    except Exception:
        results['goplus'] = False

    return results


def _health_loop() -> None:
    """Run health checks periodically."""
    time.sleep(60)  # First check after 60 seconds
    while True:
        results = check_health()
        down = [k for k, v in results.items() if not v]
        if down:
            write_log(f'HEALTH | Components DOWN: {", ".join(down)}')
        time.sleep(300)  # Every 5 minutes


def start_health_monitor() -> None:
    """Start health monitoring in a background thread."""
    thread = threading.Thread(target=_health_loop, daemon=True)
    thread.start()
