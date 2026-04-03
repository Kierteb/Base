"""
Position lifecycle and exit strategy state machine.
Monitors open positions every 12 seconds and executes exits based on:
1. Stop loss (always fires immediately, even during min hold)
2. Trailing stop (delayed by min_hold_seconds)
3. Take profit (staged exits at TP1 and TP2)
4. Time exit (force close after MAX_HOLD_HOURS)
5. Volume death (close if h1 txns drop below threshold)
"""

import json
import os
import time
from datetime import datetime, timezone

from config.constants import (
    EXIT_CONFIG_PATH, POSITION_FRACTION, MAX_POSITION_PCT,
    DAILY_LOSS_LIMIT_ETH, DAILY_LOSS_LIMIT_PCT, MAX_HOLD_HOURS,
    MIN_STOP_LOSS_PCT, MIN_TRAILING_STOP_PCT, TRADING_ENABLED,
)
from monitoring.logger import (
    get_open_positions, update_position, close_position,
    get_or_create_daily_pnl, update_daily_pnl, get_position_count,
    insert_position, write_log,
)
from signals.dexscreener import fetch_token_data
from trading.executor import sell_token


def _load_exit_config() -> dict:
    try:
        with open(EXIT_CONFIG_PATH, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get_exit_params(alert_type: str) -> dict:
    """Get exit parameters for the given alert type (early/established)."""
    config = _load_exit_config()
    section = config.get(alert_type, config.get('early', {}))
    return {k: v.get('value', v) if isinstance(v, dict) else v
            for k, v in section.items()
            if k not in ('description', 'mode')}


def calculate_position_size(wallet_balance: float, alert_type: str = 'early') -> float:
    """
    Calculate ETH to spend on a trade.
    Max 5% of wallet per trade (no absolute ETH cap).
    """
    config = _load_exit_config()
    section = config.get(alert_type, {})
    fraction = section.get('position_fraction', {})
    if isinstance(fraction, dict):
        frac = fraction.get('value', POSITION_FRACTION)
    else:
        frac = fraction or POSITION_FRACTION

    return wallet_balance * frac


def can_open_position(wallet_balance: float) -> tuple[bool, str]:
    """
    Check if we can open a new position.
    Checks daily loss limits. No max concurrent positions limit.
    """
    if not TRADING_ENABLED:
        return False, 'Trading disabled'

    # Check market regime
    try:
        from signals.market_regime import is_buying_blocked
        if is_buying_blocked():
            return False, 'Market regime: buying blocked (ETH dumping)'
    except ImportError:
        pass

    # Check daily loss limit
    daily = get_or_create_daily_pnl(wallet_balance)
    if daily['realized_pnl_eth'] <= -DAILY_LOSS_LIMIT_ETH:
        return False, f'Daily loss limit hit: {daily["realized_pnl_eth"]:.4f} ETH (limit: -{DAILY_LOSS_LIMIT_ETH})'

    starting = daily.get('starting_balance_eth', wallet_balance)
    if starting > 0:
        loss_pct = abs(daily['realized_pnl_eth']) / starting * 100
        if loss_pct >= DAILY_LOSS_LIMIT_PCT:
            return False, f'Daily loss % limit hit: {loss_pct:.1f}% (limit: {DAILY_LOSS_LIMIT_PCT}%)'

    return True, ''


def open_position(contract_address: str, symbol: str, alert_type: str,
                  entry_price_usd: float, entry_eth: float, token_amount: str,
                  entry_tx: str, score: int = 0, safety_score: int = 0) -> int | None:
    """Open a new position in the database."""
    position_id = insert_position(
        contract_address=contract_address,
        symbol=symbol,
        alert_type=alert_type,
        entry_price_usd=entry_price_usd,
        entry_eth=entry_eth,
        token_amount=token_amount,
        entry_tx=entry_tx,
        score_at_entry=score,
        safety_score=safety_score,
    )

    if position_id:
        write_log(f'POSITION | Opened #{position_id} — {symbol} ({contract_address[:12]}...) '
                  f'| {entry_eth:.4f} ETH | Score: {score}')

    return position_id


def monitor_positions() -> None:
    """
    Check all open positions for exit conditions.
    Runs every 12 seconds on the dedicated trading executor.
    Priority: emergency > stop loss > trailing stop > take profit > time exit > volume death
    """
    positions = get_open_positions()
    if not positions:
        return

    # Batch fetch current prices
    addresses = [p['contract_address'] for p in positions]
    dex_data = fetch_token_data(addresses)

    now = datetime.now(timezone.utc)

    for pos in positions:
        address = pos['contract_address']
        dex = dex_data.get(address.lower())

        if not dex:
            # No price data — track consecutive failures
            _handle_no_data(pos)
            continue

        current_price = dex.get('price_usd', 0)
        if current_price <= 0:
            continue

        entry_price = pos.get('entry_price_usd', 0)
        if entry_price <= 0:
            continue

        # Update current price and high water mark
        high_price = max(pos.get('high_price_usd', entry_price), current_price)
        update_position(pos['id'],
                        current_price_usd=current_price,
                        high_price_usd=high_price)

        pnl_pct = ((current_price - entry_price) / entry_price) * 100
        drop_from_high = ((high_price - current_price) / high_price) * 100 if high_price > 0 else 0

        # Get exit params for this position type
        alert_type = pos.get('alert_type', 'early')
        params = _get_exit_params(alert_type)

        # Calculate hold time
        opened_at = pos.get('opened_at', '')
        hold_seconds = 0
        if opened_at:
            try:
                opened_dt = datetime.fromisoformat(opened_at)
                if opened_dt.tzinfo is None:
                    opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                hold_seconds = (now - opened_dt).total_seconds()
            except (ValueError, TypeError):
                pass

        # === PRIORITY 1: STOP LOSS (always fires immediately) ===
        stop_loss = max(params.get('stop_loss_pct', 18), MIN_STOP_LOSS_PCT)
        if pnl_pct <= -stop_loss:
            _execute_exit(pos, 'stop_loss', sell_pct=100,
                          reason=f'Stop loss hit: {pnl_pct:.1f}% (limit: -{stop_loss}%)')
            continue

        # === PRIORITY 2: TRAILING STOP (delayed by min_hold) ===
        min_hold = params.get('min_hold_seconds', 300)
        trailing_pct = max(params.get('trailing_pct', 25), MIN_TRAILING_STOP_PCT)
        trailing_activation = params.get('trailing_activation_pct', 20)

        if hold_seconds >= min_hold and pnl_pct >= trailing_activation:
            if drop_from_high >= trailing_pct:
                _execute_exit(pos, 'trailing_stop', sell_pct=100,
                              reason=f'Trailing stop: -{drop_from_high:.1f}% from high '
                                     f'(high: ${high_price:.8f}, now: ${current_price:.8f})')
                continue

        # === PRIORITY 3: TAKE PROFIT (staged exits) ===
        status = pos.get('status', 'open')

        if status == 'open' and 'tp1_pct' in params:
            tp1 = params.get('tp1_pct', 60)
            if pnl_pct >= tp1:
                tp1_sell = params.get('tp1_sell_fraction', 0.5)
                _execute_exit(pos, 'tp1', sell_pct=tp1_sell * 100,
                              reason=f'TP1 hit: +{pnl_pct:.1f}% (target: +{tp1}%)')
                continue

        if status == 'partial_1' and 'tp2_pct' in params:
            tp2 = params.get('tp2_pct', 150)
            if pnl_pct >= tp2:
                tp2_sell = params.get('tp2_sell_fraction', 0.25)
                _execute_exit(pos, 'tp2', sell_pct=tp2_sell * 100,
                              reason=f'TP2 hit: +{pnl_pct:.1f}% (target: +{tp2}%)')
                continue

        # === PRIORITY 4: TIME EXIT ===
        hold_hours = hold_seconds / 3600
        if hold_hours >= MAX_HOLD_HOURS:
            _execute_exit(pos, 'time_exit', sell_pct=100,
                          reason=f'Max hold time exceeded: {hold_hours:.1f}h (limit: {MAX_HOLD_HOURS}h)')
            continue

        # === PRIORITY 5: VOLUME DEATH ===
        volume_death = params.get('volume_death_txns', 20)
        h1_buys = dex.get('h1_buys', 0)
        h1_sells = dex.get('h1_sells', 0)
        total_h1_txns = h1_buys + h1_sells

        if total_h1_txns < volume_death and hold_seconds > 600:
            _execute_exit(pos, 'volume_death', sell_pct=100,
                          reason=f'Volume death: {total_h1_txns} txns in 1h (threshold: {volume_death})')
            continue


def _execute_exit(pos: dict, exit_type: str, sell_pct: float, reason: str) -> None:
    """Execute a sell and update position."""
    position_id = pos['id']
    symbol = pos.get('symbol', pos['contract_address'][:12])

    write_log(f'POSITION | #{position_id} {symbol} — {exit_type}: {reason}')

    # Execute the sell
    result = sell_token(
        token_address=pos['contract_address'],
        sell_pct=sell_pct,
        dex_id=pos.get('dex_id', 'uniswap'),
    )

    if result.get('success'):
        eth_received = result.get('eth_received', 0)
        entry_eth = pos.get('entry_eth', 0)

        if sell_pct >= 100:
            # Full close
            total_received = pos.get('total_eth_received', 0) + eth_received
            close_position(position_id, reason=exit_type, total_eth_received=total_received)

            # Update daily P&L
            pnl = total_received - entry_eth
            update_daily_pnl(pnl)

            write_log(f'POSITION | #{position_id} {symbol} CLOSED — '
                      f'{exit_type} | P&L: {pnl:+.6f} ETH | Received: {total_received:.6f} ETH')
        else:
            # Partial exit
            new_status = 'partial_1' if pos.get('status') == 'open' else 'partial_2'
            total_received = pos.get('total_eth_received', 0) + eth_received
            sold_pct = pos.get('sold_pct', 0) + sell_pct

            exit_txs = pos.get('exit_txs', [])
            exit_txs.append(result.get('tx_hash', ''))

            update_position(position_id,
                            status=new_status,
                            total_eth_received=total_received,
                            sold_pct=sold_pct,
                            exit_txs=json.dumps(exit_txs))

            write_log(f'POSITION | #{position_id} {symbol} PARTIAL EXIT — '
                      f'{exit_type} | Sold {sell_pct:.0f}% | ETH received: {eth_received:.6f}')
    else:
        write_log(f'POSITION | #{position_id} {symbol} — SELL FAILED: {result.get("error")}')


def _handle_no_data(pos: dict) -> None:
    """Handle positions where we can't get price data (possible rug)."""
    # After 3 cycles with no data (~36 seconds), emergency sell
    no_data_key = f'_no_data_count_{pos["id"]}'
    count = getattr(_handle_no_data, no_data_key, 0) + 1
    setattr(_handle_no_data, no_data_key, count)

    if count >= 3:
        _execute_exit(pos, 'emergency_no_data', sell_pct=100,
                      reason=f'No price data for {count} cycles — possible rug')
        setattr(_handle_no_data, no_data_key, 0)


def retry_failed_trades() -> None:
    """Retry trades that failed execution. Runs every 2 minutes."""
    # Placeholder — will be implemented when we have a failed trades queue
    pass


def reconcile_positions() -> None:
    """
    Check on-chain token balances vs open positions.
    Closes positions where tokens were sold externally or rugged.
    Runs every 5 minutes.
    """
    from trading.wallet import get_token_balance

    positions = get_open_positions()
    for pos in positions:
        balance = get_token_balance(pos['contract_address'])
        if balance <= 0 and pos.get('status') in ('open', 'partial_1', 'partial_2'):
            close_position(pos['id'], reason='external_sell', total_eth_received=pos.get('total_eth_received', 0))
            write_log(f'RECONCILE | #{pos["id"]} {pos.get("symbol", "?")} — '
                      f'Token balance is 0, closed as external_sell')


def get_daily_summary(date: str) -> dict:
    """Get daily trading summary for notifications."""
    daily = get_or_create_daily_pnl(0)
    open_count = get_position_count()
    return {
        'starting_balance_eth': daily.get('starting_balance_eth', 0),
        'realized_pnl_eth': daily.get('realized_pnl_eth', 0),
        'trades_count': daily.get('trades_count', 0),
        'open_positions': open_count,
    }
