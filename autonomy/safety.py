"""
IMMUTABLE safety guardrails for the Base chain trading bot.
These values CANNOT be changed by the strategist.

The strategist can modify scoring_config.json, exit_config.json, and most
Python files — but NOT this file, wallet.py, executor.py, or main.py.
"""

# === POSITION LIMITS ===
# Max 5% of wallet per trade (no absolute ETH cap — scales with wallet)
MAX_POSITION_PCT = 5.0

# === DAILY LIMITS ===
# Pause all new trades if daily realised loss exceeds either of these
DAILY_LOSS_LIMIT_ETH = 0.5
DAILY_LOSS_LIMIT_PCT = 15.0

# === EXIT FLOORS ===
# Stop loss can never be tighter than 18% (meme coins swing 15% routinely)
MIN_STOP_LOSS_PCT = 18.0
# Trailing stop can never be tighter than 10%
MIN_TRAILING_STOP_PCT = 10.0

# === HOLD LIMITS ===
# Force close any position older than 24 hours
MAX_HOLD_HOURS = 24
# Tighten trailing stop on positions older than 12 hours
STALE_POSITION_HOURS = 12

# === EMERGENCY ===
# Emergency sell after N consecutive cycles with no price data
EMERGENCY_NO_DATA_CYCLES = 3

# === STRATEGIST RESTRICTIONS ===
# Files the strategist is NOT allowed to modify
IMMUTABLE_FILES = {
    'autonomy/safety.py',
    'trading/wallet.py',
    'trading/executor.py',
    'main.py',
    'config/.env',
}

# Functions/imports the strategist is NOT allowed to use in generated code
BANNED_PATTERNS = {
    'eval(', 'exec(', 'subprocess', 'os.remove', 'os.rmdir',
    'shutil.rmtree', '__import__', 'importlib.import_module',
    'open(.env', 'PRIVATE_KEY',
}


def validate_exit_config(config: dict) -> tuple[bool, list[str]]:
    """
    Validate that an exit config doesn't violate safety guardrails.
    Called before the strategist applies config changes.
    """
    errors = []

    for layer in ['early', 'established']:
        section = config.get(layer, {})

        # Check stop loss floor
        sl = section.get('stop_loss_pct', {})
        sl_value = sl.get('value', 18) if isinstance(sl, dict) else sl
        if sl_value < MIN_STOP_LOSS_PCT:
            errors.append(f'{layer}.stop_loss_pct = {sl_value} < {MIN_STOP_LOSS_PCT} minimum')

        # Check trailing stop floor
        ts = section.get('trailing_pct', {})
        ts_value = ts.get('value', 10) if isinstance(ts, dict) else ts
        if ts_value < MIN_TRAILING_STOP_PCT:
            errors.append(f'{layer}.trailing_pct = {ts_value} < {MIN_TRAILING_STOP_PCT} minimum')

        # Check position size cap
        pf = section.get('position_fraction', {})
        pf_value = pf.get('value', 0.05) if isinstance(pf, dict) else pf
        if pf_value > MAX_POSITION_PCT / 100:
            errors.append(f'{layer}.position_fraction = {pf_value} > {MAX_POSITION_PCT / 100} max')

    return len(errors) == 0, errors


def validate_code_change(file_path: str, code: str) -> tuple[bool, list[str]]:
    """
    Validate that a proposed code change is safe.
    Called before the strategist deploys code changes.
    """
    errors = []

    # Check if file is immutable
    for immutable in IMMUTABLE_FILES:
        if file_path.endswith(immutable):
            errors.append(f'{file_path} is immutable — strategist cannot modify it')
            return False, errors

    # Check for banned patterns
    for pattern in BANNED_PATTERNS:
        if pattern in code:
            errors.append(f'Banned pattern found: {pattern}')

    return len(errors) == 0, errors
