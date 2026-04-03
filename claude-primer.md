# Base Chain Meme Coin Scanner — Claude Primer

## What This Is
A 24/7 automated trading bot on Base chain (Coinbase L2) that identifies profitable meme coin trades using smart wallet tracking, on-chain safety analysis, and momentum signals. Accumulates ETH as base currency.

## Architecture
- **Language:** Python 3.12
- **Scheduler:** APScheduler with ThreadPoolExecutor (4 default workers + 1 dedicated trading worker)
- **Database:** SQLite with WAL mode at `data/scanner.db`
- **Dashboard:** HTTP on port 8082
- **Server:** Hetzner VPS (89.167.99.61), systemd service `base-scanner.service`

## Key Flows
1. **Discovery:** DexScreener polling (30s) + new pool monitoring (120s) → register tokens
2. **Safety:** 16-filter chain (GoPlus, Honeypot.is, Basescan, LP lock, holder analysis) → pass/fail
3. **Scoring:** 21 weighted signals across 7 categories → score 0-200, buy threshold 65
4. **Execution:** Uniswap V3 / Aerodrome via web3.py, dynamic slippage (up to 10% buy, 20% sell)
5. **Exits:** Position monitor every 12s — stop loss (18% floor, always fires), trailing stop, TP1/TP2, time exit, volume death

## Safety Guardrails (IMMUTABLE — in autonomy/safety.py)
- Max 5% of wallet per trade
- Daily loss limit: 0.5 ETH or 15%
- Stop loss floor: 18%
- Trailing stop floor: 10%
- Force close after 24 hours
- No concurrent position limit

## APIs
- Alchemy (Base RPC + wallet tracking)
- Basescan (contract verification, deployer history)
- GoPlus Security (honeypot, holder analysis, proxy detection)
- Honeypot.is (sell simulation)
- DexScreener (prices, volumes, new pairs)
- Telegram Bot (notifications)

## Config Files (strategist-managed)
- `data/scoring_config.json` — signal weights with min/max bounds
- `data/exit_config.json` — exit strategy parameters
- `data/smart_wallets.json` — tracked wallet addresses

## Current Status
- Fresh build, not yet deployed
- No trade history — scoring weights are initial estimates
- Smart wallet list is empty — needs discovery cycle to populate
