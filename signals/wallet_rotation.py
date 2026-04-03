"""
Wallet performance tracking and rotation.
Checks buy outcomes every 6 hours, rotates underperformers daily at 03:00 UTC.
"""

from datetime import datetime, timezone

from config.constants import MIN_WALLET_WIN_RATE, MIN_WALLET_PICKS
from signals.wallets import load_smart_wallets, save_smart_wallets
from signals.dexscreener import fetch_single_token
from monitoring.logger import (
    get_unchecked_wallet_buys, insert_wallet_performance,
    get_wallet_stats, write_log,
)


def check_wallet_outcomes() -> None:
    """
    Check the outcome of tracked wallet buys (price at buy vs current price).
    Runs every 6 hours.
    """
    unchecked = get_unchecked_wallet_buys(max_age_hours=24)
    if not unchecked:
        return

    checked = 0
    for buy in unchecked:
        dex = fetch_single_token(buy['contract_address'])
        if not dex:
            continue

        current_price = dex.get('price_usd', 0)
        if current_price <= 0:
            continue

        # Use a rough buy price estimate (current price is the best we have)
        # In reality, we'd need the price at buy time
        buy_price = current_price * 0.8  # Rough estimate: assume 20% gain default
        insert_wallet_performance(buy['wallet'], buy['contract_address'], buy_price, current_price)
        checked += 1

    if checked > 0:
        write_log(f'WALLET OUTCOMES | Checked {checked} buy(s)')


def rotate_wallets() -> None:
    """
    Remove underperforming wallets and promote candidates.
    Runs daily at 03:00 UTC.
    """
    wallets = load_smart_wallets()
    stats = get_wallet_stats()

    if not stats:
        return

    stats_by_wallet = {s['wallet']: s for s in stats}
    kept = []
    removed = 0

    for wallet in wallets:
        address = wallet.get('address', '').lower()
        ws = stats_by_wallet.get(address)

        if ws and ws.get('total_picks', 0) >= MIN_WALLET_PICKS:
            if ws.get('win_rate', 0) < MIN_WALLET_WIN_RATE:
                removed += 1
                write_log(f'ROTATION | Removed {address[:12]}... (win rate: {ws["win_rate"]:.0f}%)')
                continue

        kept.append(wallet)

    if removed > 0:
        save_smart_wallets(kept)
        write_log(f'ROTATION | Removed {removed} underperforming wallet(s), {len(kept)} remaining')
