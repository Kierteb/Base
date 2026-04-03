"""
Config rollback on losing days.
Snapshots scoring/exit configs on profitable days.
Rolls back to last profitable config after consecutive losing days.
"""

import json
import os
import shutil
from datetime import datetime, timezone

from config.constants import SCORING_CONFIG_PATH, EXIT_CONFIG_PATH
from monitoring.logger import write_log

SNAPSHOT_DIR = os.path.join(os.path.dirname(SCORING_CONFIG_PATH), 'config_snapshots')


def maybe_snapshot_on_profit() -> None:
    """
    Snapshot current configs if today was profitable.
    Called at end of day (23:55 UTC).
    """
    try:
        from monitoring.logger import _ensure_db
        conn = _ensure_db()
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        row = conn.execute(
            'SELECT realized_pnl_eth FROM daily_pnl WHERE date = ?', (today,)
        ).fetchone()
        conn.close()

        if row and row[0] > 0:
            _save_snapshot(today)
            write_log(f'ROLLBACK | Saved config snapshot for profitable day {today}')
    except Exception as e:
        write_log(f'ROLLBACK | Snapshot error: {e}')


def _save_snapshot(date: str) -> None:
    """Save current configs to snapshot directory."""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    for src, name in [(SCORING_CONFIG_PATH, 'scoring'), (EXIT_CONFIG_PATH, 'exit')]:
        if os.path.exists(src):
            dst = os.path.join(SNAPSHOT_DIR, f'{name}_{date}.json')
            shutil.copy2(src, dst)
