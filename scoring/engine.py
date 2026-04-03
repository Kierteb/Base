"""
Scoring engine for Base chain tokens.
Fresh design — not ported from Solana scanner.
Reads signal weights from data/scoring_config.json (strategist-managed).
"""

import json
import os
from datetime import datetime, timezone

from config.constants import SCORING_CONFIG_PATH, ALERT_THRESHOLD, MIN_SIGNAL_TYPES
from monitoring.logger import (
    get_token_alert_count, get_last_alert_time, insert_alert,
    insert_alert_tracking, write_log,
)

# Wallet buy cache — populated by wallet_cycle, consumed by scoring
_wallet_buys_cache: dict[str, list[dict]] = {}


def update_wallet_buys_cache(contract_address: str, buys: list[dict]) -> None:
    """Update the in-memory wallet buys cache (called from signals.wallets)."""
    _wallet_buys_cache[contract_address.lower()] = buys


def _load_config() -> dict:
    try:
        with open(SCORING_CONFIG_PATH, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get_signal(config: dict, name: str) -> dict:
    return config.get('signals', {}).get(name, {})


def _is_enabled(sig: dict) -> bool:
    return sig.get('enabled', False)


def _points(sig: dict) -> int:
    return sig.get('points', {}).get('value', 0)


def score_token(token: dict, dex_data: dict | None,
                safety_penalties: list[tuple[str, int]] | None = None) -> dict:
    """
    Calculate composite score for a Base chain token.

    Args:
        token: Database token record
        dex_data: DexScreener data
        safety_penalties: Soft penalties/bonuses from safety filter chain

    Returns:
        dict with score, breakdown, should_alert, etc.
    """
    config = _load_config()
    threshold = config.get('alert_threshold', {}).get('value', ALERT_THRESHOLD)
    min_types = config.get('min_signal_types', {}).get('value', MIN_SIGNAL_TYPES)

    score = 0
    signal_types: set[str] = set()
    breakdown: list[dict] = []
    address = token['contract_address']

    # === SMART WALLET SIGNALS ===
    wallet_buys = _wallet_buys_cache.get(address.lower(), [])
    unique_wallets = len(set(b.get('wallet', '') for b in wallet_buys))

    sig = _get_signal(config, 'smart_wallet_1')
    if _is_enabled(sig) and unique_wallets >= 1:
        max_buy = max((b.get('amount_eth', 0) for b in wallet_buys), default=0)
        pts = _points(sig) // 2 if max_buy < 0.02 else _points(sig)
        score += pts
        signal_types.add('smart_wallet')
        breakdown.append({
            'signal': f'Smart wallet buy ({unique_wallets} wallet{"s" if unique_wallets > 1 else ""}, {max_buy:.4f} ETH)',
            'points': pts,
        })

        sig2 = _get_signal(config, 'smart_wallet_2_plus')
        if _is_enabled(sig2) and unique_wallets >= 2:
            pts2 = _points(sig2)
            score += pts2
            breakdown.append({'signal': 'Multiple smart wallets bonus', 'points': pts2})

    if not dex_data:
        return _build_result(score, signal_types, breakdown, threshold, min_types, address, token)

    # === VOLUME SIGNALS ===
    h1_vol = dex_data.get('h1_volume', 0)
    h24_vol = dex_data.get('h24_volume', 0)
    h24_avg_h1 = h24_vol / 24 if h24_vol > 0 else 0

    sig = _get_signal(config, 'volume_spike_10x')
    if _is_enabled(sig) and h24_avg_h1 > 0 and h1_vol >= h24_avg_h1 * 10:
        pts = _points(sig)
        score += pts
        signal_types.add('volume')
        breakdown.append({'signal': f'Volume spike 10x ({h1_vol / h24_avg_h1:.1f}x)', 'points': pts})
    else:
        sig = _get_signal(config, 'volume_spike_5x')
        if _is_enabled(sig) and h24_avg_h1 > 0 and h1_vol >= h24_avg_h1 * 5:
            pts = _points(sig)
            score += pts
            signal_types.add('volume')
            breakdown.append({'signal': f'Volume spike 5x ({h1_vol / h24_avg_h1:.1f}x)', 'points': pts})

    sig = _get_signal(config, 'sustained_volume')
    if _is_enabled(sig) and h1_vol >= 50_000 and h24_vol >= 200_000:
        pts = _points(sig)
        score += pts
        signal_types.add('volume')
        breakdown.append({'signal': f'Sustained volume (h1: ${h1_vol:,.0f}, h24: ${h24_vol:,.0f})', 'points': pts})

    # === PRICE SIGNALS ===
    h1_change = dex_data.get('h1_price_change', 0)

    sig = _get_signal(config, 'price_pump_100')
    if _is_enabled(sig) and h1_change >= 100:
        pts = _points(sig)
        score += pts
        signal_types.add('price')
        breakdown.append({'signal': f'Price pump +{h1_change:.0f}% in 1h', 'points': pts})
    else:
        sig = _get_signal(config, 'price_acceleration_30')
        if _is_enabled(sig) and h1_change >= 30:
            pts = _points(sig)
            score += pts
            signal_types.add('price')
            breakdown.append({'signal': f'Price acceleration +{h1_change:.0f}% in 1h', 'points': pts})

    # === BUYING PRESSURE ===
    h1_buys = dex_data.get('h1_buys', 0)
    h1_sells = dex_data.get('h1_sells', 0)
    ratio = dex_data.get('buy_sell_ratio', 0)

    sig = _get_signal(config, 'buy_sell_ratio_3')
    if _is_enabled(sig) and ratio >= 3:
        pts = _points(sig)
        score += pts
        signal_types.add('buying_pressure')
        breakdown.append({'signal': f'Buy/sell ratio {ratio:.1f}:1 ({h1_buys}b/{h1_sells}s)', 'points': pts})
    else:
        sig = _get_signal(config, 'buy_sell_ratio_2')
        if _is_enabled(sig) and ratio >= 2:
            pts = _points(sig)
            score += pts
            signal_types.add('buying_pressure')
            breakdown.append({'signal': f'Buy/sell ratio {ratio:.1f}:1', 'points': pts})

    # === ORGANIC INTEREST ===
    total_h1_txns = h1_buys + h1_sells

    sig = _get_signal(config, 'high_txn_count')
    if _is_enabled(sig) and total_h1_txns >= 200:
        pts = _points(sig)
        score += pts
        signal_types.add('organic')
        breakdown.append({'signal': f'{total_h1_txns} transactions in 1h', 'points': pts})

    sig = _get_signal(config, 'organic_buying')
    if _is_enabled(sig) and h1_buys >= 20 and h1_vol > 0:
        avg_buy_size = h1_vol / h1_buys if h1_buys > 0 else 0
        if avg_buy_size < 200:
            pts = _points(sig)
            score += pts
            signal_types.add('organic')
            breakdown.append({'signal': f'Organic buying (avg ${avg_buy_size:.0f}, {h1_buys} buys)', 'points': pts})

    # === MARKET CAP ===
    mcap = dex_data.get('market_cap', 0)

    sig = _get_signal(config, 'mcap_sweet_spot')
    if _is_enabled(sig) and 100_000 <= mcap <= 5_000_000:
        pts = _points(sig)
        score += pts
        signal_types.add('market_cap')
        breakdown.append({'signal': f'MCap sweet spot (${mcap:,.0f})', 'points': pts})

    # === SAFETY BONUSES/PENALTIES ===
    if safety_penalties:
        for name, pts in safety_penalties:
            sig = _get_signal(config, name)
            if sig and _is_enabled(sig):
                actual_pts = _points(sig)
            else:
                actual_pts = pts

            score += actual_pts
            category = sig.get('category', 'safety_bonus' if actual_pts > 0 else 'penalty')
            if category not in signal_types and actual_pts != 0:
                signal_types.add(category)
            breakdown.append({'signal': name.replace('_', ' ').title(), 'points': actual_pts})

    return _build_result(score, signal_types, breakdown, threshold, min_types, address, token)


def _build_result(score: int, signal_types: set, breakdown: list, threshold: int,
                  min_types: int, address: str, token: dict) -> dict:
    """Build the final scoring result."""
    score = max(0, min(score, 200))  # Clamp 0-200
    meets_threshold = score >= threshold and len(signal_types) >= min_types

    # Check alert limits
    should_alert = False
    if meets_threshold:
        from config.constants import MAX_ALERTS_PER_TOKEN, ALERT_COOLDOWN_SECONDS
        alert_count = get_token_alert_count(address)
        if alert_count < MAX_ALERTS_PER_TOKEN:
            last_alert = get_last_alert_time()
            if last_alert:
                try:
                    last_dt = datetime.fromisoformat(last_alert)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                    should_alert = elapsed >= ALERT_COOLDOWN_SECONDS
                except (ValueError, TypeError):
                    should_alert = True
            else:
                should_alert = True

    return {
        'score': score,
        'signal_types_count': len(signal_types),
        'signal_types': list(signal_types),
        'breakdown': breakdown,
        'meets_threshold': meets_threshold,
        'should_alert': should_alert,
        'threshold': threshold,
        'min_signal_types': min_types,
    }
