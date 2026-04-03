"""
Telegram alert sending for token scoring alerts (not trade notifications).
"""

import requests

from config.constants import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from monitoring.logger import insert_alert, insert_alert_tracking, write_log


def send_alert(token: dict, score_result: dict, dex_data: dict | None) -> bool:
    """
    Send a scoring alert via Telegram when a token crosses the threshold.
    Also logs the alert in the database for performance tracking.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    address = token.get('contract_address', '')
    symbol = token.get('symbol', address[:12])
    score = score_result.get('score', 0)
    breakdown = score_result.get('breakdown', [])

    # Build breakdown text
    breakdown_lines = []
    for item in breakdown[:8]:  # Limit to 8 signals
        pts = item.get('points', 0)
        signal = item.get('signal', '')
        prefix = '+' if pts > 0 else ''
        breakdown_lines.append(f'  {prefix}{pts} — {signal}')

    breakdown_text = '\n'.join(breakdown_lines)

    price = dex_data.get('price_usd', 0) if dex_data else 0
    liquidity = dex_data.get('liquidity_usd', 0) if dex_data else 0
    mcap = dex_data.get('market_cap', 0) if dex_data else 0
    h1_change = dex_data.get('h1_price_change', 0) if dex_data else 0
    dex_url = dex_data.get('url', '') if dex_data else ''

    message = (
        f'🎯 <b>BASE ALERT: {symbol}</b> — Score {score}/100\n\n'
        f'<code>{address}</code>\n\n'
        f'<b>Signals:</b>\n{breakdown_text}\n\n'
        f'Price: ${price:.8f} ({h1_change:+.1f}% 1h)\n'
        f'Liquidity: ${liquidity:,.0f}\n'
        f'MCap: ${mcap:,.0f}\n'
    )

    if dex_url:
        message += f'\n<a href="{dex_url}">DexScreener</a> | '
    message += f'<a href="https://basescan.org/token/{address}">Basescan</a>'

    try:
        resp = requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
            json={
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            },
            timeout=10,
        )

        if resp.ok:
            # Log alert in database
            insert_alert(address, score, message[:500])
            insert_alert_tracking(address, symbol, 'scoring', score, price)
            return True
        else:
            write_log(f'TELEGRAM | Alert send failed: {resp.text}')
            return False

    except requests.RequestException as e:
        write_log(f'TELEGRAM | Alert send error: {e}')
        return False
