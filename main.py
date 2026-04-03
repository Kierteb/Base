# HOW TO RUN: python main.py  (from the base-scanner directory)
# Make sure config/.env exists with your API keys first.
# Copy config/.env.example to config/.env and fill in the values.
"""
Base Chain Meme Coin Scanner — Main entry point.
Monitors Base chain DEXes, smart wallets, and new token launches
to detect profitable meme coin trades.

Signal streams feed a scoring engine with safety-first filtering:
1. DexScreener polling (every 30s) — volume, price, buy/sell data for Base pairs
2. Smart wallet polling (every 60s) — tracked wallet buys via Alchemy
3. New pool monitoring (every 120s) — Uniswap V3 / Aerodrome pool creation
4. Safety rescan (every 300s) — re-check GoPlus/honeypot for active tokens

Scoring engine runs after each poll. Tokens crossing 65/100 and passing
all 16 safety filters trigger a buy.
"""

import json
import os
import signal as sig
import sys
import time
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.executors.pool import ThreadPoolExecutor as APThreadPoolExecutor

from config.constants import (
    DEXSCREENER_POLL_INTERVAL, WALLET_POLL_INTERVAL, NEW_TOKEN_POLL_INTERVAL,
    SAFETY_RESCAN_INTERVAL, POSITION_MONITOR_INTERVAL,
    TOKEN_MAX_AGE_HOURS, STALE_SCORE_THRESHOLD, ZERO_VOLUME_DEAD_MINUTES,
    ALERT_THRESHOLD, MIN_SIGNAL_TYPES, TRADING_ENABLED, DASHBOARD_PORT,
)
from signals.dexscreener import fetch_token_data
from monitoring.logger import (
    get_active_tokens, update_token, insert_score, get_scanner_stats,
    write_log, _ensure_db,
)

_start_time = None


# === Core scanning cycles ===

def dexscreener_cycle() -> None:
    """
    Poll DexScreener for all active tokens, score them, and trigger buys.
    Runs every 30 seconds.
    """
    try:
        tokens = get_active_tokens()
        if not tokens:
            return

        tokens = tokens[:200]
        addresses = [t['contract_address'] for t in tokens]
        dex_data = fetch_token_data(addresses)

        alerted = 0
        scored = 0

        for token in tokens:
            address = token['contract_address']
            dex = dex_data.get(address.lower())

            # Update token with latest DexScreener data
            if dex:
                update_token(address,
                             liquidity_usd=dex.get('liquidity_usd', 0),
                             market_cap_usd=dex.get('market_cap', 0))

            # Score the token
            try:
                from scoring.engine import score_token
                result = score_token(token, dex)
            except ImportError:
                continue

            scored += 1
            insert_score(address, result['score'], result['signal_types_count'], result['breakdown'])

            if result.get('should_alert'):
                try:
                    from alerts.telegram import send_alert
                    sent = send_alert(token, result, dex)
                    if sent:
                        alerted += 1
                except ImportError:
                    pass

            _check_token_staleness(token, result, dex)

        if alerted > 0:
            write_log(f'CYCLE | Scored {scored} tokens, sent {alerted} alert(s)')

    except Exception as e:
        print(f'  [ERROR] DexScreener cycle failed: {e}')
        write_log(f'ERROR | DexScreener cycle: {e}')
        import traceback
        traceback.print_exc()


def wallet_cycle() -> None:
    """
    Poll smart wallets for new token buys on Base.
    Runs every 60 seconds.
    """
    try:
        from signals.wallets import poll_all_wallets
        new_buys = poll_all_wallets()
        if new_buys:
            for buy in new_buys:
                print(f'  [Wallet] {buy["wallet"][:8]}... bought {buy["contract_address"][:12]}... ({buy["amount_eth"]:.4f} ETH)')
    except ImportError:
        pass
    except Exception as e:
        print(f'  [ERROR] Wallet cycle failed: {e}')
        write_log(f'ERROR | Wallet cycle: {e}')
        import traceback
        traceback.print_exc()


def new_token_cycle() -> None:
    """
    Monitor for new token pools on Base (Uniswap V3, Aerodrome).
    Runs every 120 seconds.
    """
    try:
        from signals.new_tokens import discover_new_tokens
        new_tokens = discover_new_tokens()
        if new_tokens:
            write_log(f'NEW TOKENS | Discovered {len(new_tokens)} new token(s) on Base')
    except ImportError:
        pass
    except Exception as e:
        print(f'  [ERROR] New token cycle failed: {e}')
        write_log(f'ERROR | New token cycle: {e}')


def safety_rescan_cycle() -> None:
    """
    Re-scan active tokens through safety filters (GoPlus, honeypot).
    Runs every 5 minutes. Catches tokens that become honeypots after launch.
    """
    try:
        from safety.filter_chain import rescan_active_tokens
        flagged = rescan_active_tokens()
        if flagged:
            write_log(f'SAFETY | Flagged {len(flagged)} token(s) on rescan')
    except ImportError:
        pass
    except Exception as e:
        write_log(f'ERROR | Safety rescan: {e}')


def market_regime_check() -> None:
    """
    Check ETH price for market regime filter.
    Runs every 5 minutes.
    """
    try:
        from signals.market_regime import update_regime
        update_regime()
    except ImportError:
        pass
    except Exception as e:
        write_log(f'ERROR | Market regime check: {e}')


def cleanup_cycle() -> None:
    """Mark old tokens as expired. Runs every 5 minutes."""
    try:
        tokens = get_active_tokens()
        now = datetime.now(timezone.utc)
        cleaned = 0

        for token in tokens:
            first_seen = token.get('first_seen', '')
            if not first_seen:
                continue
            try:
                seen_dt = datetime.fromisoformat(first_seen)
                if seen_dt.tzinfo is None:
                    seen_dt = seen_dt.replace(tzinfo=timezone.utc)
                age_hours = (now - seen_dt).total_seconds() / 3600
            except (ValueError, TypeError):
                continue

            if age_hours > TOKEN_MAX_AGE_HOURS:
                update_token(token['contract_address'], status='expired')
                cleaned += 1

        if cleaned > 0:
            write_log(f'CLEANUP | Marked {cleaned} token(s) as expired (>{TOKEN_MAX_AGE_HOURS}h old)')
    except Exception as e:
        write_log(f'ERROR | Cleanup cycle: {e}')


def _check_token_staleness(token: dict, score_result: dict, dex_data: dict | None) -> None:
    """Mark tokens as stale or dead based on score and volume."""
    address = token['contract_address']

    if dex_data and dex_data.get('h1_volume', 0) == 0 and dex_data.get('h24_volume', 0) == 0:
        first_seen = token.get('first_seen', '')
        if first_seen:
            try:
                seen_dt = datetime.fromisoformat(first_seen)
                if seen_dt.tzinfo is None:
                    seen_dt = seen_dt.replace(tzinfo=timezone.utc)
                age_min = (datetime.now(timezone.utc) - seen_dt).total_seconds() / 60
                if age_min > ZERO_VOLUME_DEAD_MINUTES:
                    update_token(address, status='dead')
                    return
            except (ValueError, TypeError):
                pass

    first_seen = token.get('first_seen', '')
    if first_seen:
        try:
            seen_dt = datetime.fromisoformat(first_seen)
            if seen_dt.tzinfo is None:
                seen_dt = seen_dt.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - seen_dt).total_seconds() / 3600
            if age_hours > TOKEN_MAX_AGE_HOURS and score_result['score'] < STALE_SCORE_THRESHOLD:
                update_token(address, status='stale')
        except (ValueError, TypeError):
            pass


def print_status() -> None:
    """Print a status line to the console."""
    stats = get_scanner_stats()

    uptime = ''
    if _start_time:
        elapsed = datetime.now(timezone.utc) - _start_time
        hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        uptime = f'{hours}h{minutes}m'

    print(f'  [{uptime}] Tokens: {stats["active_tokens"]} active | '
          f'Alerts today: {stats["alerts_today"]} | '
          f'Open positions: {stats["open_positions"]} | '
          f'Closed today: {stats["closed_today"]}')


# === Main entry point ===

def main() -> None:
    global _start_time
    _start_time = datetime.now(timezone.utc)

    conn = _ensure_db()
    conn.close()

    print()
    print(f'  ========================================')
    print(f'  Base Chain Meme Coin Scanner')
    print(f'  ========================================')
    print(f'  DexScreener poll:  every {DEXSCREENER_POLL_INTERVAL}s')
    print(f'  Wallet poll:       every {WALLET_POLL_INTERVAL}s')
    print(f'  New token poll:    every {NEW_TOKEN_POLL_INTERVAL}s')
    print(f'  Safety rescan:     every {SAFETY_RESCAN_INTERVAL}s')
    print(f'  Position monitor:  every {POSITION_MONITOR_INTERVAL}s')
    print(f'  Alert threshold:   {ALERT_THRESHOLD}/100 ({MIN_SIGNAL_TYPES}+ signal types)')
    print(f'  Token max age:     {TOKEN_MAX_AGE_HOURS}h')
    print(f'  Trading:           {"LIVE" if TRADING_ENABLED else "DISABLED"}')
    print(f'  ========================================')
    print()

    # Run one DexScreener cycle immediately
    tokens = get_active_tokens()
    if tokens:
        print(f'  Running initial scan on {len(tokens)} active token(s)...')
        dexscreener_cycle()
    else:
        print('  No active tokens yet — waiting for token discovery...')
    print()

    # Start web dashboard
    try:
        from monitoring.web import start_dashboard_thread
        start_dashboard_thread(DASHBOARD_PORT)
        print(f'  Dashboard: http://localhost:{DASHBOARD_PORT}')
    except ImportError:
        print('  Dashboard: not yet implemented')

    print()

    # Scheduler with dedicated trading executor
    executors = {
        'default': APThreadPoolExecutor(max_workers=4),
        'trading': APThreadPoolExecutor(max_workers=1),
    }
    scheduler = BlockingScheduler(timezone='UTC', executors=executors)

    # --- Core scanning jobs ---

    scheduler.add_job(
        dexscreener_cycle, 'interval',
        seconds=DEXSCREENER_POLL_INTERVAL,
        id='dexscreener_cycle',
        name='DexScreener poll + scoring',
    )

    scheduler.add_job(
        wallet_cycle, 'interval',
        seconds=WALLET_POLL_INTERVAL,
        id='wallet_cycle',
        name='Smart wallet poll',
    )

    scheduler.add_job(
        new_token_cycle, 'interval',
        seconds=NEW_TOKEN_POLL_INTERVAL,
        id='new_token_cycle',
        name='New token discovery',
    )

    scheduler.add_job(
        safety_rescan_cycle, 'interval',
        seconds=SAFETY_RESCAN_INTERVAL,
        id='safety_rescan',
        name='Safety filter rescan',
    )

    scheduler.add_job(
        market_regime_check, 'interval',
        minutes=5,
        id='market_regime',
        name='ETH market regime check',
    )

    scheduler.add_job(
        cleanup_cycle, 'interval',
        minutes=5,
        id='cleanup_cycle',
        name='Token cleanup',
    )

    # --- Wallet discovery and rotation ---

    scheduler.add_job(
        lambda: _safe_import_run('signals.wallet_discovery', 'run_discovery_cycle'),
        'interval', hours=4,
        id='wallet_discovery',
        name='Wallet discovery cycle',
    )

    scheduler.add_job(
        lambda: _safe_import_run('signals.wallet_rotation', 'check_wallet_outcomes'),
        'interval', hours=6,
        id='wallet_outcomes',
        name='Check wallet buy outcomes',
    )

    scheduler.add_job(
        lambda: _safe_import_run('signals.wallet_rotation', 'rotate_wallets'),
        'cron', hour=3, minute=0,
        id='wallet_rotation',
        name='Daily wallet rotation',
    )

    # --- Alert tracking ---

    scheduler.add_job(
        lambda: _safe_import_run('scoring.alert_tracker', 'check_alert_prices'),
        'interval', minutes=30,
        id='alert_price_check',
        name='Alert performance price check',
    )

    # --- Trading jobs (only when enabled) ---

    if TRADING_ENABLED:
        scheduler.add_job(
            lambda: _safe_import_run('trading.positions', 'monitor_positions'),
            'interval',
            seconds=POSITION_MONITOR_INTERVAL,
            id='position_monitor',
            name='Position monitor (exit strategy)',
            executor='trading',
        )

        scheduler.add_job(
            lambda: _safe_import_run('trading.positions', 'retry_failed_trades'),
            'interval', minutes=2,
            id='retry_failed_trades',
            name='Retry failed trade executions',
            executor='trading',
        )

        scheduler.add_job(
            lambda: _safe_import_run('trading.positions', 'reconcile_positions'),
            'interval', minutes=5,
            id='reconcile_positions',
            name='Position reconciliation',
            executor='trading',
        )

        # Daily trading summary at 23:55 UTC
        scheduler.add_job(
            _daily_trading_summary, 'cron',
            hour=23, minute=55,
            id='daily_trading_summary',
            name='Daily trading P/L summary',
        )

    # --- Strategist (autonomous AI brain) ---

    scheduler.add_job(
        lambda: _safe_import_run('autonomy.strategist', 'scheduled_run'),
        'interval', minutes=30,
        id='strategist_cycle',
        name='Strategist — autonomous AI brain',
        max_instances=1,
        misfire_grace_time=300,
        coalesce=True,
    )

    # --- Status line ---

    scheduler.add_job(
        print_status, 'interval',
        minutes=2,
        id='status_print',
        name='Console status',
    )

    # Graceful shutdown
    def shutdown(signum, frame):
        print('\n\n  Shutting down gracefully...')
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        time.sleep(3)
        os._exit(0)

    try:
        sig.signal(sig.SIGINT, shutdown)
        sig.signal(sig.SIGTERM, shutdown)
    except ValueError:
        pass

    print(f'  Scheduler started. Press Ctrl+C to stop.\n')
    print_status()
    print()

    scheduler.start()


def _safe_import_run(module_path: str, func_name: str) -> None:
    """Safely import and run a function. Logs errors but doesn't crash the scheduler."""
    try:
        import importlib
        mod = importlib.import_module(module_path)
        fn = getattr(mod, func_name)
        fn()
    except ImportError as e:
        write_log(f'IMPORT | {module_path}.{func_name} not yet implemented: {e}')
    except Exception as e:
        write_log(f'ERROR | {module_path}.{func_name}: {e}')
        import traceback
        traceback.print_exc()


def _daily_trading_summary() -> None:
    """Send daily P&L summary via Telegram."""
    try:
        from trading.positions import get_daily_summary
        from trading.wallet import get_eth_balance
        from trading.notifications import send_daily_summary

        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        summary = get_daily_summary(today)
        balance = get_eth_balance()

        send_daily_summary(
            date=today,
            starting_balance=summary.get('starting_balance_eth', balance),
            current_balance=balance,
            trades_today=summary.get('trades_count', 0),
            realized_pnl=summary.get('realized_pnl_eth', 0),
            open_positions=summary.get('open_positions', 0),
        )
    except ImportError:
        write_log('SUMMARY | Trading modules not yet implemented')
    except Exception as e:
        write_log(f'ERROR | Daily summary: {e}')


if __name__ == '__main__':
    main()
