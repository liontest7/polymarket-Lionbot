# Polymarket Multi-Asset Trading Bot v4.1 PRO

## Overview
A fully automated crypto trading bot for Polymarket 5-minute Up/Down markets. Supports 8 assets simultaneously (BTC, ETH, SOL, XRP, MATIC, DOGE, LINK, AVAX). Entry is allowed **at any time** during a 5-minute window when signal conditions are met — not just the last 25 seconds.

## Architecture

### Core Stack
- **Python 3.11** with asyncio
- **FastAPI + uvicorn** on port 5000 (web dashboard)
- **WebSocket** for real-time dashboard updates

### Data Feeds
- **`src/data/binance_feed.py`** — `MultiAssetFeed`: Streams real-time prices for 8 assets via Binance combined WebSocket
- **`src/data/polymarket_feed.py`** — `MultiMarketFeed`: Discovers 5-min markets on Polymarket. Falls back to simulation mode if Polymarket API is unreachable

### Signal & Execution
- **`src/signal/engine.py`** — `SignalEngine`: Entry allowed ANY time in the window. Uses multi-timeframe delta (20s + 60s), price velocity filter, cooldown per asset, and EV-ranked candidates
- **`src/execution/executor.py`** — `PaperExecutor` / `LiveExecutor`: Paper mode has TP/SL monitoring + time stop. Live mode uses py-clob-client with limit orders
- **`src/risk/manager.py`** — `RiskManager`: Dynamic Kelly sizing, consecutive loss protection, open trade cap (max 3), daily loss limits, drawdown reduction
- **`src/bot.py`** — `PolyBot`: Orchestrates all components

### API & Dashboard
- **`src/api/server.py`** — FastAPI with REST + WebSocket. Endpoints: `/api/bot/start`, `/api/bot/stop`, `/api/state`, `/api/settings`
- **`web/templates/dashboard.html`** — Real-time dashboard

### Configuration
- **`config/settings.py`** — Pydantic settings from `.env` / environment variables
- **`.env.example`** — Template with all settings documented

## Running
```
python run.py           # Paper/demo mode (auto-starts bot)
python run.py --live    # Live mode (requires API keys)
```

## Signal Strategy (v4.1)
- **Entry**: ANY TIME in the 5-minute window (minimum 10s remaining)
- **Conditions**: delta > 0.04% AND velocity > 0.0015%/s AND edge > 2.5% after fees
- **Multi-timeframe**: 20s + 60s delta must be consistent for confidence bonus
- **Direction**: exploits Binance → Polymarket lag
- **Cooldown**: 30s per asset after any trade

## Risk Management (v4.1)
- **Kelly sizing**: Dynamic 20-35% fractional Kelly based on edge strength
- **Max open trades**: 3 simultaneously
- **Consecutive losses**: Reduce size after 2+, stop after 5
- **Drawdown**: Reduce at 10%, halve at 15%, stop at 25%
- **Daily loss limit**: 5% of capital

## Exit Management (v4.1 NEW)
- **Take Profit (TP)**: Exit when implied token price gains +0.12 (configurable)
- **Stop Loss (SL)**: Exit when implied token price falls -0.08 (configurable)
- **Time Stop**: Force exit after 120s if no TP/SL (configurable)
- **Window Close**: Resolve at window settlement if none of the above triggered

## Stats Tracked
- Win rate, avg win, avg loss, expected value per trade
- Daily PnL, total PnL, fees paid
- Consecutive losses, drawdown, capital curve

## Modes
- **Paper/Demo**: No API keys needed. Real Binance prices, simulated Polymarket markets.
- **Live**: Requires Polymarket API keys. Places real LIMIT orders via py-clob-client.

## Key Settings (via dashboard Settings panel or .env)
| Setting | Default | Description |
|---|---|---|
| MIN_BTC_DELTA_PCT | 0.04 | Min price move % in 20s |
| MIN_VELOCITY_PCT_PER_SEC | 0.0015 | Min speed filter |
| MIN_EDGE_AFTER_FEES | 0.025 | Min edge to trade |
| TP_TOKEN_GAIN | 0.12 | Take profit target |
| SL_TOKEN_LOSS | 0.08 | Stop loss target |
| TIME_STOP_SECONDS | 120 | Force exit time |
| COOLDOWN_SECONDS | 30 | Per-asset cooldown |
| MAX_CONSECUTIVE_LOSSES | 5 | Stop trading after N losses |

## Ports
- `5000` — Web dashboard (webview)
