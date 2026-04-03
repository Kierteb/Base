"""
HTTP dashboard for Base Scanner.
Lightweight — raw http.server, no frameworks.
Runs on port 8082 alongside Solana scanner's 8081.
"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

from monitoring.logger import (
    get_scanner_stats, get_open_positions, get_closed_positions,
    get_recent_alerts, get_active_tokens, get_wallet_stats,
)
from trading.wallet import get_eth_balance


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/' or path == '/dashboard':
            self._serve_dashboard()
        elif path == '/api/stats':
            self._serve_json(get_scanner_stats())
        elif path == '/api/positions':
            self._serve_json({
                'open': get_open_positions(),
                'closed': get_closed_positions(limit=20),
            })
        elif path == '/api/alerts':
            self._serve_json({'alerts': get_recent_alerts(limit=30)})
        elif path == '/api/tokens':
            self._serve_json({'tokens': get_active_tokens()[:50]})
        elif path == '/api/wallets':
            self._serve_json({'wallets': get_wallet_stats()})
        else:
            self.send_error(404)

    def _serve_json(self, data: dict):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_dashboard(self):
        stats = get_scanner_stats()
        positions = get_open_positions()
        closed = get_closed_positions(limit=10)
        alerts = get_recent_alerts(limit=10)

        # Wallet balance
        try:
            wallet_balance = get_eth_balance()
        except Exception:
            wallet_balance = 0.0

        # Calculate total P&L from closed positions
        total_pnl = sum(p.get('pnl_eth', 0) for p in closed)
        wins = sum(1 for p in closed if p.get('pnl_eth', 0) > 0)
        losses = sum(1 for p in closed if p.get('pnl_eth', 0) <= 0)
        win_rate = (wins / len(closed) * 100) if closed else 0

        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Base Scanner Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="30">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Inter', -apple-system, sans-serif; background: #0a1628; color: #e2e8f0; padding: 1.5rem; }}
        h1 {{ font-size: 1.5rem; margin-bottom: 1rem; color: #f97316; }}
        h2 {{ font-size: 1.1rem; margin: 1.5rem 0 0.75rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }}
        .card {{ background: #1e293b; padding: 1rem; border-radius: 0.5rem; }}
        .card .label {{ font-size: 0.75rem; color: #64748b; text-transform: uppercase; }}
        .card .value {{ font-size: 1.5rem; font-weight: 700; margin-top: 0.25rem; }}
        .green {{ color: #22c55e; }}
        .red {{ color: #ef4444; }}
        .orange {{ color: #f97316; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 0.5rem; }}
        th, td {{ padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid #1e293b; font-size: 0.85rem; }}
        th {{ color: #64748b; font-weight: 600; text-transform: uppercase; font-size: 0.75rem; }}
        .mono {{ font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; }}
        a {{ color: #60a5fa; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .updated {{ color: #475569; font-size: 0.75rem; margin-top: 1.5rem; }}
    </style>
</head>
<body>
    <h1>Base Chain Scanner</h1>

    <div class="grid">
        <div class="card">
            <div class="label">Wallet Balance</div>
            <div class="value">{wallet_balance:.4f} ETH</div>
        </div>
        <div class="card">
            <div class="label">Active Tokens</div>
            <div class="value">{stats['active_tokens']}</div>
        </div>
        <div class="card">
            <div class="label">Alerts Today</div>
            <div class="value orange">{stats['alerts_today']}</div>
        </div>
        <div class="card">
            <div class="label">Open Positions</div>
            <div class="value">{stats['open_positions']}</div>
        </div>
        <div class="card">
            <div class="label">Total P&L</div>
            <div class="value {'green' if total_pnl >= 0 else 'red'}">{total_pnl:+.4f} ETH</div>
        </div>
        <div class="card">
            <div class="label">Win Rate</div>
            <div class="value {'green' if win_rate >= 50 else 'red'}">{win_rate:.0f}%</div>
        </div>
        <div class="card">
            <div class="label">Trades (W/L)</div>
            <div class="value">{wins}/{losses}</div>
        </div>
    </div>

    <h2>Open Positions</h2>
    <table>
        <tr><th>Token</th><th>Entry</th><th>Current</th><th>P&L</th><th>Hold Time</th><th>Score</th></tr>
        {''.join(_position_row(p) for p in positions) or '<tr><td colspan="6" style="color:#475569">No open positions</td></tr>'}
    </table>

    <h2>Recent Closed</h2>
    <table>
        <tr><th>Token</th><th>P&L (ETH)</th><th>P&L %</th><th>Reason</th><th>Closed</th></tr>
        {''.join(_closed_row(p) for p in closed) or '<tr><td colspan="5" style="color:#475569">No closed positions yet</td></tr>'}
    </table>

    <h2>Recent Alerts</h2>
    <table>
        <tr><th>Token</th><th>Score</th><th>Time</th></tr>
        {''.join(_alert_row(a) for a in alerts) or '<tr><td colspan="3" style="color:#475569">No alerts yet</td></tr>'}
    </table>

    <div class="updated">Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} | Auto-refreshes every 30s</div>
</body>
</html>"""

        body = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress access logs


def _position_row(p: dict) -> str:
    entry = p.get('entry_price_usd', 0)
    current = p.get('current_price_usd', 0)
    pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
    color = 'green' if pnl_pct >= 0 else 'red'

    opened = p.get('opened_at', '')[:16]
    symbol = p.get('symbol', p['contract_address'][:8])
    addr = p['contract_address']

    return (
        f'<tr>'
        f'<td><a href="https://basescan.org/token/{addr}" target="_blank">{symbol}</a></td>'
        f'<td class="mono">${entry:.8f}</td>'
        f'<td class="mono">${current:.8f}</td>'
        f'<td class="{color}">{pnl_pct:+.1f}%</td>'
        f'<td>{opened}</td>'
        f'<td>{p.get("score_at_entry", 0)}</td>'
        f'</tr>'
    )


def _closed_row(p: dict) -> str:
    color = 'green' if p.get('pnl_eth', 0) >= 0 else 'red'
    symbol = p.get('symbol', p['contract_address'][:8])

    return (
        f'<tr>'
        f'<td>{symbol}</td>'
        f'<td class="{color}">{p.get("pnl_eth", 0):+.6f}</td>'
        f'<td class="{color}">{p.get("pnl_pct", 0):+.1f}%</td>'
        f'<td>{p.get("close_reason", "")}</td>'
        f'<td>{(p.get("closed_at") or "")[:16]}</td>'
        f'</tr>'
    )


def _alert_row(a: dict) -> str:
    symbol = a.get('symbol', a.get('contract_address', '')[:8])
    return (
        f'<tr>'
        f'<td>{symbol}</td>'
        f'<td class="orange">{a.get("score", 0)}</td>'
        f'<td>{(a.get("sent_at") or "")[:16]}</td>'
        f'</tr>'
    )


def start_dashboard_thread(port: int = 8082) -> None:
    """Start the dashboard HTTP server in a background thread."""
    server = HTTPServer(('0.0.0.0', port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
