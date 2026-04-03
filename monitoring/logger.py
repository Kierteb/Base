"""
Base Scanner logging and database.
Logs all tokens, signals, scores, positions, and alerts to SQLite.
"""

import sqlite3
import json
import os
import time
import threading
from datetime import datetime, timezone, timedelta

from config.constants import DB_PATH, LOG_DIR

_db_initialized = False
_db_init_lock = threading.Lock()


def _ensure_db() -> sqlite3.Connection:
    """Create the database and tables if they don't exist. Uses WAL mode for concurrent access."""
    global _db_initialized
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')

    if _db_initialized:
        return conn

    with _db_init_lock:
        if _db_initialized:
            return conn

        conn.execute('''
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT UNIQUE NOT NULL,
                symbol TEXT,
                name TEXT,
                deployer TEXT,
                decimals INTEGER DEFAULT 18,
                total_supply TEXT,
                liquidity_usd REAL,
                market_cap_usd REAL,
                first_seen TEXT NOT NULL,
                last_updated TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                dex_id TEXT,
                pool_address TEXT,
                pair_address TEXT,
                is_verified INTEGER DEFAULT 0,
                is_proxy INTEGER DEFAULT 0,
                is_honeypot INTEGER DEFAULT 0,
                buy_tax REAL DEFAULT 0,
                sell_tax REAL DEFAULT 0,
                owner_address TEXT,
                ownership_renounced INTEGER DEFAULT 0,
                is_mintable INTEGER DEFAULT 0,
                lp_locked_pct REAL DEFAULT 0,
                top3_holder_pct REAL DEFAULT 0,
                holder_count INTEGER DEFAULT 0,
                goplus_last_check TEXT,
                safety_score INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_value REAL,
                details TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (contract_address) REFERENCES tokens(contract_address)
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL,
                score INTEGER NOT NULL,
                signal_types_count INTEGER NOT NULL,
                breakdown TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (contract_address) REFERENCES tokens(contract_address)
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL,
                score INTEGER NOT NULL,
                message TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                FOREIGN KEY (contract_address) REFERENCES tokens(contract_address)
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL,
                symbol TEXT,
                alert_type TEXT,
                entry_price_usd REAL,
                entry_eth REAL,
                token_amount TEXT,
                current_price_usd REAL,
                high_price_usd REAL,
                status TEXT DEFAULT 'open',
                sold_pct REAL DEFAULT 0,
                total_eth_received REAL DEFAULT 0,
                stop_loss_pct REAL DEFAULT -18,
                trailing_stop_high REAL,
                entry_tx TEXT,
                exit_txs TEXT DEFAULT '[]',
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                close_reason TEXT,
                score_at_entry INTEGER,
                safety_score INTEGER,
                FOREIGN KEY (contract_address) REFERENCES tokens(contract_address)
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS daily_pnl (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                starting_balance_eth REAL,
                realized_pnl_eth REAL DEFAULT 0,
                trades_count INTEGER DEFAULT 0
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS wallet_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                contract_address TEXT NOT NULL,
                action TEXT NOT NULL,
                amount_eth REAL,
                tx_hash TEXT UNIQUE,
                timestamp TEXT NOT NULL
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS wallet_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                contract_address TEXT NOT NULL,
                buy_price_usd REAL,
                check_price_usd REAL,
                price_change_pct REAL,
                is_win INTEGER,
                checked_at TEXT NOT NULL,
                UNIQUE(wallet, contract_address)
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS alert_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL,
                symbol TEXT,
                alert_type TEXT NOT NULL,
                score INTEGER,
                price_at_alert REAL,
                price_1h REAL,
                price_6h REAL,
                price_24h REAL,
                change_1h REAL,
                change_6h REAL,
                change_24h REAL,
                alerted_at TEXT NOT NULL,
                last_checked TEXT,
                UNIQUE(contract_address, alert_type, alerted_at)
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS discovery_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT UNIQUE NOT NULL,
                source TEXT,
                discovered_at TEXT NOT NULL,
                picks_count INTEGER DEFAULT 0,
                wins_count INTEGER DEFAULT 0,
                promoted INTEGER DEFAULT 0,
                promoted_at TEXT
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS safety_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL,
                filter_name TEXT NOT NULL,
                passed INTEGER NOT NULL,
                reason TEXT,
                severity TEXT,
                checked_at TEXT NOT NULL
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS strategist_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_type TEXT NOT NULL,
                cycle_number INTEGER,
                context_summary TEXT,
                response_summary TEXT,
                actions_taken TEXT,
                tokens_used INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        ''')

        # Indexes for frequent queries
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tokens_status ON tokens(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tokens_address ON tokens(contract_address)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_signals_address ON signals(contract_address)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_scores_address ON scores(contract_address)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_alerts_address ON alerts(contract_address)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_wallet_activity_wallet ON wallet_activity(wallet)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_wallet_perf_wallet ON wallet_performance(wallet)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_alert_tracking_address ON alert_tracking(contract_address)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_safety_checks_address ON safety_checks(contract_address)')

        conn.commit()
        _db_initialized = True
    return conn


def _db_write(fn, *args, max_retries: int = 3):
    """Execute a write operation with retry on database lock errors."""
    for attempt in range(max_retries):
        conn = None
        try:
            conn = _ensure_db()
            result = fn(conn, *args)
            conn.commit()
            return result
        except sqlite3.OperationalError as e:
            if 'locked' in str(e) and attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            write_log(f'DB | Write error after {attempt + 1} attempts: {e}')
        except Exception as e:
            write_log(f'DB | Unexpected error: {e}')
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    return None


# === Token operations ===

def insert_token(contract_address: str, symbol: str, name: str, deployer: str = '',
                 liquidity_usd: float = 0, dex_id: str = '', pool_address: str = '',
                 pair_address: str = '') -> None:
    now = datetime.now(timezone.utc).isoformat()

    def _write(conn, *_args):
        conn.execute(
            '''INSERT OR IGNORE INTO tokens
               (contract_address, symbol, name, deployer, liquidity_usd, dex_id,
                pool_address, pair_address, first_seen, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (contract_address, symbol, name, deployer, liquidity_usd, dex_id,
             pool_address, pair_address, now, now)
        )
    _db_write(_write)


def update_token(contract_address: str, **kwargs) -> None:
    now = datetime.now(timezone.utc).isoformat()
    kwargs['last_updated'] = now

    def _write(conn, *_args):
        set_clause = ', '.join(f'{k} = ?' for k in kwargs)
        values = list(kwargs.values()) + [contract_address]
        conn.execute(f'UPDATE tokens SET {set_clause} WHERE contract_address = ?', values)
    _db_write(_write)


def get_active_tokens() -> list[dict]:
    conn = _ensure_db()
    cursor = conn.execute(
        """SELECT contract_address, symbol, name, deployer, liquidity_usd, market_cap_usd,
                  dex_id, pool_address, first_seen, last_updated, safety_score
           FROM tokens WHERE status = 'active' ORDER BY first_seen DESC"""
    )
    tokens = [
        {
            'contract_address': r[0], 'symbol': r[1], 'name': r[2], 'deployer': r[3],
            'liquidity_usd': r[4], 'market_cap_usd': r[5], 'dex_id': r[6],
            'pool_address': r[7], 'first_seen': r[8], 'last_updated': r[9],
            'safety_score': r[10],
        }
        for r in cursor.fetchall()
    ]
    conn.close()
    return tokens


def get_token(contract_address: str) -> dict | None:
    conn = _ensure_db()
    cursor = conn.execute(
        'SELECT * FROM tokens WHERE contract_address = ?', (contract_address,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    cols = [desc[0] for desc in cursor.description]
    return dict(zip(cols, row))


def token_exists(contract_address: str) -> bool:
    conn = _ensure_db()
    count = conn.execute(
        'SELECT COUNT(*) FROM tokens WHERE contract_address = ?', (contract_address,)
    ).fetchone()[0]
    conn.close()
    return count > 0


# === Signal operations ===

def insert_signal(contract_address: str, signal_type: str, signal_value: float, details: str = '') -> None:
    now = datetime.now(timezone.utc).isoformat()

    def _write(conn, *_args):
        conn.execute(
            'INSERT INTO signals (contract_address, signal_type, signal_value, details, timestamp) VALUES (?, ?, ?, ?, ?)',
            (contract_address, signal_type, signal_value, details, now)
        )
    _db_write(_write)


# === Score operations ===

def insert_score(contract_address: str, score: int, signal_types_count: int, breakdown: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()

    def _write(conn, *_args):
        conn.execute(
            'INSERT INTO scores (contract_address, score, signal_types_count, breakdown, timestamp) VALUES (?, ?, ?, ?, ?)',
            (contract_address, score, signal_types_count, json.dumps(breakdown), now)
        )
    _db_write(_write)


def get_latest_score(contract_address: str) -> dict | None:
    conn = _ensure_db()
    cursor = conn.execute(
        'SELECT score, signal_types_count, breakdown, timestamp FROM scores WHERE contract_address = ? ORDER BY id DESC LIMIT 1',
        (contract_address,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {'score': row[0], 'signal_types_count': row[1], 'breakdown': json.loads(row[2]), 'timestamp': row[3]}
    return None


# === Alert operations ===

def insert_alert(contract_address: str, score: int, message: str) -> None:
    now = datetime.now(timezone.utc).isoformat()

    def _write(conn, *_args):
        conn.execute(
            'INSERT INTO alerts (contract_address, score, message, sent_at) VALUES (?, ?, ?, ?)',
            (contract_address, score, message, now)
        )
    _db_write(_write)


def get_token_alert_count(contract_address: str) -> int:
    conn = _ensure_db()
    count = conn.execute(
        'SELECT COUNT(*) FROM alerts WHERE contract_address = ?', (contract_address,)
    ).fetchone()[0]
    conn.close()
    return count


def get_last_alert_time() -> str | None:
    conn = _ensure_db()
    row = conn.execute('SELECT sent_at FROM alerts ORDER BY id DESC LIMIT 1').fetchone()
    conn.close()
    return row[0] if row else None


def get_recent_alerts(limit: int = 20) -> list[dict]:
    conn = _ensure_db()
    cursor = conn.execute(
        '''SELECT a.contract_address, COALESCE(t.symbol, at.symbol) as symbol, a.score, a.message, a.sent_at
           FROM alerts a
           LEFT JOIN tokens t ON a.contract_address = t.contract_address
           LEFT JOIN alert_tracking at ON a.contract_address = at.contract_address
           ORDER BY a.id DESC LIMIT ?''',
        (limit,)
    )
    alerts = [
        {'contract_address': r[0], 'symbol': r[1], 'score': r[2], 'message': r[3], 'sent_at': r[4]}
        for r in cursor.fetchall()
    ]
    conn.close()
    return alerts


# === Position operations ===

def insert_position(contract_address: str, symbol: str, alert_type: str,
                    entry_price_usd: float, entry_eth: float, token_amount: str,
                    entry_tx: str, score_at_entry: int = 0, safety_score: int = 0) -> int | None:
    now = datetime.now(timezone.utc).isoformat()

    def _write(conn, *_args):
        cursor = conn.execute(
            '''INSERT INTO positions
               (contract_address, symbol, alert_type, entry_price_usd, entry_eth, token_amount,
                current_price_usd, high_price_usd, entry_tx, opened_at, score_at_entry, safety_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (contract_address, symbol, alert_type, entry_price_usd, entry_eth, token_amount,
             entry_price_usd, entry_price_usd, entry_tx, now, score_at_entry, safety_score)
        )
        return cursor.lastrowid
    return _db_write(_write)


def get_open_positions() -> list[dict]:
    conn = _ensure_db()
    cursor = conn.execute(
        """SELECT id, contract_address, symbol, alert_type, entry_price_usd, entry_eth,
                  token_amount, current_price_usd, high_price_usd, status, sold_pct,
                  total_eth_received, stop_loss_pct, trailing_stop_high, entry_tx,
                  exit_txs, opened_at, score_at_entry, safety_score
           FROM positions WHERE status IN ('open', 'partial_1', 'partial_2')
           ORDER BY opened_at DESC"""
    )
    positions = []
    for r in cursor.fetchall():
        positions.append({
            'id': r[0], 'contract_address': r[1], 'symbol': r[2], 'alert_type': r[3],
            'entry_price_usd': r[4], 'entry_eth': r[5], 'token_amount': r[6],
            'current_price_usd': r[7], 'high_price_usd': r[8], 'status': r[9],
            'sold_pct': r[10], 'total_eth_received': r[11], 'stop_loss_pct': r[12],
            'trailing_stop_high': r[13], 'entry_tx': r[14],
            'exit_txs': json.loads(r[15]) if r[15] else [],
            'opened_at': r[16], 'score_at_entry': r[17], 'safety_score': r[18],
        })
    conn.close()
    return positions


def update_position(position_id: int, **kwargs) -> None:
    def _write(conn, *_args):
        set_clause = ', '.join(f'{k} = ?' for k in kwargs)
        values = list(kwargs.values()) + [position_id]
        conn.execute(f'UPDATE positions SET {set_clause} WHERE id = ?', values)
    _db_write(_write)


def close_position(position_id: int, reason: str, total_eth_received: float = 0) -> None:
    now = datetime.now(timezone.utc).isoformat()

    def _write(conn, *_args):
        conn.execute(
            'UPDATE positions SET status = ?, close_reason = ?, closed_at = ?, total_eth_received = ? WHERE id = ?',
            ('closed', reason, now, total_eth_received, position_id)
        )
    _db_write(_write)


def get_closed_positions(limit: int = 50) -> list[dict]:
    conn = _ensure_db()
    cursor = conn.execute(
        """SELECT id, contract_address, symbol, alert_type, entry_price_usd, entry_eth,
                  total_eth_received, close_reason, opened_at, closed_at, score_at_entry
           FROM positions WHERE status = 'closed'
           ORDER BY closed_at DESC LIMIT ?""",
        (limit,)
    )
    positions = []
    for r in cursor.fetchall():
        entry_eth = r[5] or 0
        received = r[6] or 0
        pnl_eth = received - entry_eth
        pnl_pct = (pnl_eth / entry_eth * 100) if entry_eth > 0 else 0
        positions.append({
            'id': r[0], 'contract_address': r[1], 'symbol': r[2], 'alert_type': r[3],
            'entry_price_usd': r[4], 'entry_eth': entry_eth, 'total_eth_received': received,
            'pnl_eth': round(pnl_eth, 6), 'pnl_pct': round(pnl_pct, 1),
            'close_reason': r[7], 'opened_at': r[8], 'closed_at': r[9],
            'score_at_entry': r[10],
        })
    conn.close()
    return positions


def get_position_count() -> int:
    conn = _ensure_db()
    count = conn.execute("SELECT COUNT(*) FROM positions WHERE status IN ('open', 'partial_1', 'partial_2')").fetchone()[0]
    conn.close()
    return count


# === Daily P&L ===

def get_or_create_daily_pnl(starting_balance: float) -> dict:
    conn = _ensure_db()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    row = conn.execute('SELECT * FROM daily_pnl WHERE date = ?', (today,)).fetchone()
    if not row:
        conn.execute(
            'INSERT INTO daily_pnl (date, starting_balance_eth) VALUES (?, ?)',
            (today, starting_balance)
        )
        conn.commit()
        row = conn.execute('SELECT * FROM daily_pnl WHERE date = ?', (today,)).fetchone()
    conn.close()
    return {'date': row[1], 'starting_balance_eth': row[2], 'realized_pnl_eth': row[3], 'trades_count': row[4]}


def update_daily_pnl(pnl_eth: float) -> None:
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    def _write(conn, *_args):
        conn.execute(
            'UPDATE daily_pnl SET realized_pnl_eth = realized_pnl_eth + ?, trades_count = trades_count + 1 WHERE date = ?',
            (pnl_eth, today)
        )
    _db_write(_write)


# === Wallet tracking ===

def insert_wallet_activity(wallet: str, contract_address: str, action: str,
                           amount_eth: float, tx_hash: str) -> None:
    now = datetime.now(timezone.utc).isoformat()

    def _write(conn, *_args):
        conn.execute(
            'INSERT OR IGNORE INTO wallet_activity (wallet, contract_address, action, amount_eth, tx_hash, timestamp) VALUES (?, ?, ?, ?, ?, ?)',
            (wallet, contract_address, action, amount_eth, tx_hash, now)
        )
    _db_write(_write)


def insert_wallet_performance(wallet: str, contract_address: str,
                              buy_price: float, check_price: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    change_pct = ((check_price - buy_price) / buy_price * 100) if buy_price > 0 else 0
    is_win = 1 if change_pct > 0 else 0

    def _write(conn, *_args):
        conn.execute(
            '''INSERT OR REPLACE INTO wallet_performance
               (wallet, contract_address, buy_price_usd, check_price_usd, price_change_pct, is_win, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (wallet, contract_address, buy_price, check_price, round(change_pct, 2), is_win, now)
        )
    _db_write(_write)


def get_wallet_stats() -> list[dict]:
    conn = _ensure_db()
    cursor = conn.execute('''
        SELECT wallet, COUNT(*) as total_picks, SUM(is_win) as wins,
               ROUND(AVG(price_change_pct), 1) as avg_change,
               ROUND(CAST(SUM(is_win) AS REAL) / COUNT(*) * 100, 0) as win_rate
        FROM wallet_performance GROUP BY wallet
        ORDER BY win_rate DESC, avg_change DESC
    ''')
    stats = [
        {'wallet': r[0], 'total_picks': r[1], 'wins': r[2], 'avg_change': r[3], 'win_rate': r[4]}
        for r in cursor.fetchall()
    ]
    conn.close()
    return stats


def get_unchecked_wallet_buys(max_age_hours: int = 24) -> list[dict]:
    conn = _ensure_db()
    cursor = conn.execute('''
        SELECT wa.wallet, wa.contract_address, wa.timestamp
        FROM wallet_activity wa
        LEFT JOIN wallet_performance wp ON wa.wallet = wp.wallet AND wa.contract_address = wp.contract_address
        WHERE wp.id IS NULL AND wa.action = 'buy'
          AND wa.timestamp > datetime('now', ?)
          AND wa.timestamp < datetime('now', '-1 hour')
    ''', (f'-{max_age_hours} hours',))
    buys = [{'wallet': r[0], 'contract_address': r[1], 'timestamp': r[2]} for r in cursor.fetchall()]
    conn.close()
    return buys


# === Alert tracking ===

def insert_alert_tracking(contract_address: str, symbol: str, alert_type: str,
                          score: int, price_at_alert: float) -> None:
    now = datetime.now(timezone.utc).isoformat()

    def _write(conn, *_args):
        conn.execute(
            '''INSERT OR IGNORE INTO alert_tracking
               (contract_address, symbol, alert_type, score, price_at_alert, alerted_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (contract_address, symbol, alert_type, score, price_at_alert, now)
        )
    _db_write(_write)


def get_unchecked_alerts(interval: str) -> list[dict]:
    conn = _ensure_db()
    col = f'price_{interval}'
    hours = {'1h': 1, '6h': 6, '24h': 24}[interval]
    cursor = conn.execute(f'''
        SELECT id, contract_address, symbol, alert_type, score, price_at_alert, alerted_at
        FROM alert_tracking
        WHERE {col} IS NULL AND alerted_at < datetime('now', ?)
    ''', (f'-{hours} hours',))
    alerts = [
        {'id': r[0], 'contract_address': r[1], 'symbol': r[2], 'alert_type': r[3],
         'score': r[4], 'price_at_alert': r[5], 'alerted_at': r[6]}
        for r in cursor.fetchall()
    ]
    conn.close()
    return alerts


def update_alert_tracking(alert_id: int, interval: str, price: float, change_pct: float) -> None:
    now = datetime.now(timezone.utc).isoformat()

    def _write(conn, *_args):
        conn.execute(
            f'UPDATE alert_tracking SET price_{interval} = ?, change_{interval} = ?, last_checked = ? WHERE id = ?',
            (price, change_pct, now, alert_id)
        )
    _db_write(_write)


# === Safety checks ===

def insert_safety_check(contract_address: str, filter_name: str, passed: bool,
                        reason: str = '', severity: str = 'info') -> None:
    now = datetime.now(timezone.utc).isoformat()

    def _write(conn, *_args):
        conn.execute(
            'INSERT INTO safety_checks (contract_address, filter_name, passed, reason, severity, checked_at) VALUES (?, ?, ?, ?, ?, ?)',
            (contract_address, filter_name, 1 if passed else 0, reason, severity, now)
        )
    _db_write(_write)


# === Discovery candidates ===

def insert_discovery_candidate(wallet: str, source: str = '') -> None:
    now = datetime.now(timezone.utc).isoformat()

    def _write(conn, *_args):
        conn.execute(
            'INSERT OR IGNORE INTO discovery_candidates (wallet, source, discovered_at) VALUES (?, ?, ?)',
            (wallet, source, now)
        )
    _db_write(_write)


def update_discovery_candidate(wallet: str, **kwargs) -> None:
    def _write(conn, *_args):
        set_clause = ', '.join(f'{k} = ?' for k in kwargs)
        values = list(kwargs.values()) + [wallet]
        conn.execute(f'UPDATE discovery_candidates SET {set_clause} WHERE wallet = ?', values)
    _db_write(_write)


# === Strategist journal ===

def insert_strategist_entry(cycle_type: str, cycle_number: int, context_summary: str,
                            response_summary: str, actions_taken: str,
                            tokens_used: int = 0, cost_usd: float = 0) -> None:
    now = datetime.now(timezone.utc).isoformat()

    def _write(conn, *_args):
        conn.execute(
            '''INSERT INTO strategist_journal
               (cycle_type, cycle_number, context_summary, response_summary, actions_taken,
                tokens_used, cost_usd, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (cycle_type, cycle_number, context_summary, response_summary, actions_taken,
             tokens_used, cost_usd, now)
        )
    _db_write(_write)


# === Scanner stats ===

def get_scanner_stats() -> dict:
    conn = _ensure_db()
    total_tokens = conn.execute('SELECT COUNT(*) FROM tokens').fetchone()[0]
    active_tokens = conn.execute("SELECT COUNT(*) FROM tokens WHERE status = 'active'").fetchone()[0]
    total_alerts = conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0]
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    alerts_today = conn.execute('SELECT COUNT(*) FROM alerts WHERE sent_at LIKE ?', (f'{today}%',)).fetchone()[0]
    open_positions = conn.execute("SELECT COUNT(*) FROM positions WHERE status IN ('open', 'partial_1', 'partial_2')").fetchone()[0]
    closed_today = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status = 'closed' AND closed_at LIKE ?", (f'{today}%',)
    ).fetchone()[0]
    conn.close()
    return {
        'total_tokens': total_tokens,
        'active_tokens': active_tokens,
        'total_alerts': total_alerts,
        'alerts_today': alerts_today,
        'open_positions': open_positions,
        'closed_today': closed_today,
    }


# === Logging ===

def get_top_scoring_tokens(limit: int = 20) -> list[dict]:
    """Get tokens with the highest recent scores, with their latest DexScreener data."""
    conn = _ensure_db()
    cursor = conn.execute('''
        SELECT s.contract_address, t.symbol, t.name, s.score, s.signal_types_count,
               s.breakdown, s.timestamp, t.liquidity_usd, t.market_cap_usd,
               t.dex_id, t.pair_address, t.safety_score
        FROM scores s
        JOIN tokens t ON s.contract_address = t.contract_address
        WHERE t.status = 'active'
          AND s.id IN (
              SELECT MAX(id) FROM scores GROUP BY contract_address
          )
        ORDER BY s.score DESC
        LIMIT ?
    ''', (limit,))
    tokens = []
    for r in cursor.fetchall():
        try:
            breakdown = json.loads(r[5]) if r[5] else []
        except (json.JSONDecodeError, TypeError):
            breakdown = []
        tokens.append({
            'contract_address': r[0], 'symbol': r[1], 'name': r[2],
            'score': r[3], 'signal_types_count': r[4], 'breakdown': breakdown,
            'last_scored': r[6], 'liquidity_usd': r[7] or 0,
            'market_cap_usd': r[8] or 0, 'dex_id': r[9] or '',
            'pair_address': r[10] or '', 'safety_score': r[11] or 0,
        })
    conn.close()
    return tokens


def write_log(message: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    log_file = os.path.join(LOG_DIR, f'{today}.log')
    timestamp = datetime.now(timezone.utc).strftime('%H:%M:%S')
    with open(log_file, 'a') as f:
        f.write(f'[{timestamp}] {message}\n')


if __name__ == '__main__':
    _ensure_db()
    stats = get_scanner_stats()
    print('Base Scanner database ready.')
    print(f'  Tokens tracked: {stats["total_tokens"]}')
    print(f'  Active tokens:  {stats["active_tokens"]}')
    print(f'  Total alerts:   {stats["total_alerts"]}')
    print(f'  Open positions: {stats["open_positions"]}')
