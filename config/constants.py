"""
Base Scanner constants and configuration.
All thresholds and parameters are defined here as the single source of truth.
"""

import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# --- Base RPC ---
ALCHEMY_API_KEY: str = os.getenv('ALCHEMY_API_KEY', '')
BASE_RPC_URL: str = os.getenv('BASE_RPC_URL', f'https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}' if ALCHEMY_API_KEY else 'https://mainnet.base.org')
BASE_WS_URL: str = os.getenv('BASE_WS_URL', f'wss://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}' if ALCHEMY_API_KEY else '')

# --- Basescan ---
BASESCAN_API_KEY: str = os.getenv('BASESCAN_API_KEY', '')
# Basescan V1 is deprecated — use Etherscan V2 or Alchemy for token transfers
BASESCAN_BASE_URL: str = 'https://api.etherscan.io/v2/api'

# --- GoPlus Security ---
GOPLUS_API_KEY: str = os.getenv('GOPLUS_API_KEY', '')
GOPLUS_BASE_URL: str = 'https://api.gopluslabs.io/api/v1'
GOPLUS_CHAIN_ID: str = '8453'  # Base mainnet

# --- Honeypot.is ---
HONEYPOT_BASE_URL: str = 'https://api.honeypot.is/v2'

# --- DexScreener ---
DEXSCREENER_BASE_URL: str = 'https://api.dexscreener.com/latest/dex'
DEXSCREENER_BATCH_SIZE: int = 30

# --- Anthropic ---
ANTHROPIC_API_KEY: str = os.getenv('ANTHROPIC_API_KEY', '')
CLAUDE_MODEL: str = 'claude-sonnet-4-20250514'

# --- Telegram ---
TELEGRAM_BOT_TOKEN: str = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID: str = os.getenv('TELEGRAM_CHAT_ID', '')

# --- Resend (email) ---
RESEND_API_KEY: str = os.getenv('RESEND_API_KEY', '')

# --- Base Chain Contracts ---
WETH_ADDRESS: str = '0x4200000000000000000000000000000000000006'
UNISWAP_V3_ROUTER: str = '0x2626664c2603336E57B271c5C0b26F421741e481'
UNISWAP_V3_FACTORY: str = '0x33128a8fC17869897dcE68Ed026d694621f6FDfD'
AERODROME_ROUTER: str = '0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43'
AERODROME_FACTORY: str = '0x420DD381b31aEf6683db6B902084cB0FFECe40Da'
BURN_ADDRESSES: set = {
    '0x0000000000000000000000000000000000000000',
    '0x000000000000000000000000000000000000dEaD',
}

# --- Safety Filter Thresholds ---
MIN_TOKEN_AGE_SECONDS: int = 1200  # 20 minutes
MIN_LIQUIDITY_USD: float = 100_000.0
MAX_BUY_TAX_PCT: float = 5.0
MAX_SELL_TAX_PCT: float = 10.0
MIN_LP_LOCKED_PCT: float = 80.0
MAX_TOP3_WALLET_PCT: float = 20.0
MIN_UNIQUE_BUYERS: int = 50
MIN_UNIQUE_SELLERS: int = 15
MIN_HOLDER_COUNT: int = 100
REQUIRE_VERIFIED_SOURCE: bool = True
REJECT_PROXY_CONTRACTS: bool = True
SAFETY_CACHE_TTL_SECONDS: int = 1800  # 30 minutes

# --- Scoring ---
ALERT_THRESHOLD: int = 45
MIN_SIGNAL_TYPES: int = 2
TOKEN_MAX_AGE_HOURS: int = 24
STALE_SCORE_THRESHOLD: int = 30
ZERO_VOLUME_DEAD_MINUTES: int = 30

# --- Anti-spam ---
ALERT_COOLDOWN_SECONDS: int = 120
MAX_ALERTS_PER_TOKEN: int = 1

# --- Polling intervals (seconds) ---
DEXSCREENER_POLL_INTERVAL: int = 30
WALLET_POLL_INTERVAL: int = 60
NEW_TOKEN_POLL_INTERVAL: int = 120
SAFETY_RESCAN_INTERVAL: int = 300
POSITION_MONITOR_INTERVAL: int = 12

# --- Trading ---
BASE_PRIVATE_KEY: str = os.getenv('BASE_PRIVATE_KEY', '')
TRADING_ENABLED: bool = os.getenv('TRADING_ENABLED', 'false').lower() == 'true'
POSITION_FRACTION: float = 0.10  # 10% of wallet per trade
MAX_POSITION_PCT: float = 10.0  # Same as above, used in safety checks
DAILY_LOSS_LIMIT_ETH: float = 0.5
DAILY_LOSS_LIMIT_PCT: float = 15.0
MAX_SINGLE_TOKEN_PCT: float = 15.0
MAX_HOLD_HOURS: int = 24
MIN_STOP_LOSS_PCT: float = 18.0
MIN_TRAILING_STOP_PCT: float = 10.0
STOP_LOSS_PCT: float = 18.0
TRAILING_STOP_PCT: float = 25.0
MAX_BUY_SLIPPAGE_PCT: float = 10.0
MAX_SELL_SLIPPAGE_PCT: float = 20.0

# --- Smart Wallet Tracking ---
WALLET_DISCOVERY_INTERVAL_HOURS: int = 4
WALLET_OUTCOMES_INTERVAL_HOURS: int = 6
MIN_WALLET_WIN_RATE: float = 40.0  # Promote candidate after 40%+ win rate
MIN_WALLET_PICKS: int = 8  # Need 8+ picks before promotion
WALLET_4H_RETURN_THRESHOLD: float = 50.0  # 50% return in last 4 hours

# --- Market Regime ---
ETH_DUMP_6H_THRESHOLD: float = -3.0  # Block new buys if ETH down >3% in 6h
ETH_DUMP_24H_THRESHOLD: float = -8.0  # Block all trades if ETH down >8% in 24h

# --- Paths ---
DB_PATH: str = os.path.join(os.path.dirname(__file__), '..', 'data', 'scanner.db')
LOG_DIR: str = os.path.join(os.path.dirname(__file__), '..', 'logs')
SMART_WALLETS_PATH: str = os.path.join(os.path.dirname(__file__), '..', 'data', 'smart_wallets.json')
SCORING_CONFIG_PATH: str = os.path.join(os.path.dirname(__file__), '..', 'data', 'scoring_config.json')
EXIT_CONFIG_PATH: str = os.path.join(os.path.dirname(__file__), '..', 'data', 'exit_config.json')
TOKEN_BLACKLIST_PATH: str = os.path.join(os.path.dirname(__file__), '..', 'data', 'token_blacklist.json')
DEPLOYER_BLACKLIST_PATH: str = os.path.join(os.path.dirname(__file__), '..', 'data', 'deployer_blacklist.json')
SAFETY_CACHE_PATH: str = os.path.join(os.path.dirname(__file__), '..', 'data', 'safety_cache.json')

# --- Dashboard ---
DASHBOARD_PORT: int = 8082
