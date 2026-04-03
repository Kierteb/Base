"""
Alert performance tracking.
Checks token prices after alerts to measure scoring accuracy.
"""

from signals.dexscreener import fetch_single_token
from monitoring.logger import get_unchecked_alerts, update_alert_tracking, write_log


def check_alert_prices() -> None:
    """
    Check prices for past alerts to track performance.
    Runs every 30 minutes.
    """
    for interval in ['1h', '6h', '24h']:
        alerts = get_unchecked_alerts(interval)
        if not alerts:
            continue

        checked = 0
        for alert in alerts:
            dex = fetch_single_token(alert['contract_address'])
            if not dex:
                continue

            current_price = dex.get('price_usd', 0)
            alert_price = alert.get('price_at_alert', 0)
            if alert_price <= 0 or current_price <= 0:
                continue

            change_pct = ((current_price - alert_price) / alert_price) * 100
            update_alert_tracking(alert['id'], interval, current_price, round(change_pct, 1))
            checked += 1

        if checked > 0:
            write_log(f'ALERT TRACKER | Checked {checked} alert(s) at {interval}')
