"""
Trading notifications via Telegram and email (Resend).
"""

import requests

from config.constants import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, RESEND_API_KEY
from monitoring.logger import write_log


def send_trade_notification(trade_type: str, symbol: str, contract_address: str,
                            eth_amount: float, price_usd: float, score: int = 0,
                            tx_hash: str = '', extra: str = '') -> None:
    """Send a buy/sell notification via Telegram."""
    emoji = '🟢' if trade_type == 'BUY' else '🔴'
    basescan_link = f'https://basescan.org/tx/{tx_hash}' if tx_hash else ''

    message = (
        f'{emoji} <b>{trade_type}: {symbol}</b>\n'
        f'Amount: {eth_amount:.4f} ETH\n'
        f'Price: ${price_usd:.8f}\n'
    )
    if score:
        message += f'Score: {score}/100\n'
    if basescan_link:
        message += f'<a href="{basescan_link}">View on Basescan</a>\n'
    if extra:
        message += f'{extra}\n'

    _send_telegram(message)


def send_daily_summary(date: str, starting_balance: float, current_balance: float,
                       trades_today: int, realized_pnl: float,
                       open_positions: int) -> None:
    """Send daily P&L summary."""
    pnl_emoji = '📈' if realized_pnl >= 0 else '📉'
    pnl_pct = (realized_pnl / starting_balance * 100) if starting_balance > 0 else 0

    message = (
        f'{pnl_emoji} <b>Daily Summary — {date}</b>\n\n'
        f'Starting: {starting_balance:.4f} ETH\n'
        f'Current:  {current_balance:.4f} ETH\n'
        f'P&L:      {realized_pnl:+.4f} ETH ({pnl_pct:+.1f}%)\n'
        f'Trades:   {trades_today}\n'
        f'Open:     {open_positions} position(s)\n'
    )

    _send_telegram(message)


def _send_telegram(message: str) -> bool:
    """Send a message via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

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
        return resp.ok
    except requests.RequestException as e:
        write_log(f'TELEGRAM | Send error: {e}')
        return False


def send_email_notification(subject: str, body: str, to: str = '') -> bool:
    """Send an email notification via Resend."""
    if not RESEND_API_KEY:
        return False

    try:
        resp = requests.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {RESEND_API_KEY}'},
            json={
                'from': 'Base Scanner <notifications@pageonedesign.co.uk>',
                'to': [to] if to else ['hello@deepwaterdesign.co.uk'],
                'subject': subject,
                'html': body,
            },
            timeout=10,
        )
        return resp.ok
    except requests.RequestException as e:
        write_log(f'RESEND | Send error: {e}')
        return False
