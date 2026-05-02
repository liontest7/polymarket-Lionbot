# Polymarket Multi-Asset Trading Bot v4

## Overview
A fully automated crypto trading bot for Polymarket 5-minute Up/Down markets. Supports 8 assets simultaneously (BTC, ETH, SOL, XRP, MATIC, DOGE, LINK, AVAX).

## Architecture

### Core Stack
- **Python 3.11** with asyncio
- **FastAPI + uvicorn** on port 5000 (web dashboard)
- **WebSocket** for real-time dashboard updates

### Data Feeds
- **`src/data/binance_feed.py`** — `MultiAssetFeed`: Streams real-time prices for 8 assets via Binance combined WebSocket
- **`src/data/polymarket_feed.py`** — `MultiMarketFeed`: Discovers 5-min markets on Polymarket. Falls back to simulation mode if Polymarket API is unreachable (e.g., Replit environment)

### Signal & Execution
- **`src/signal/engine.py`** — `SignalEngine`: Evaluates all 8 asset windows each second, picks the best edge signal
- **`src/execution/executor.py`** — `PaperExecutor` / `LiveExecutor`: Paper mode uses simulated/real Polymarket resolution; live mode uses py-clob-client
- **`src/risk/manager.py`** — `RiskManager`: Kelly Criterion sizing, daily loss limits, drawdown protection
- **`src/bot.py`** — `PolyBot`: Orchestrates all components, start/stop controllable via API

### API & Dashboard
- **`src/api/server.py`** — FastAPI with REST + WebSocket. Endpoints: `/api/bot/start`, `/api/bot/stop`, `/api/state`, `/api/settings`
- **`web/templates/dashboard.html`** — Real-time dashboard with Start/Stop button, active markets grid, P&L chart

### Configuration
- **`config/settings.py`** — Pydantic settings from `.env` file
- **`.env`** — Copy of `.env.example` with your settings

## Running
```
python run.py           # Paper/demo mode
python run.py --live    # Live mode (requires API keys)
```

## Key Features
1. **8 assets monitored simultaneously**: BTC, ETH, SOL, XRP, MATIC, DOGE, LINK, AVAX
2. **Start/Stop button** in dashboard header — no server restart needed
3. **Simulation mode** — when Polymarket API is unreachable, generates synthetic 5-min windows using real Binance prices and resolves trades against actual price movements
4. **Real-time dashboard** — live BTC price, active markets countdown, P&L chart, trade history
5. **Win Rate priority** — signal engine picks highest-confidence opportunity across all assets

## Modes
- **Paper/Demo**: No API keys needed. Uses Binance WebSocket for prices. Simulates Polymarket markets and resolves based on actual price direction.
- **Live**: Requires Polymarket API keys (Private Key, API Key/Secret/Passphrase, Alchemy key). Places real orders via py-clob-client.

## Strategy
- Entry window: last 25 seconds of each 5-minute window
- Uses BTC/asset delta (% price move in last 20s) as signal
- Estimates win probability using calibrated delta→probability table
- Edge = win_prob × 1.0 - token_price - fees
- Only trades when edge > threshold (default 0.03)
- Position sizing: fractional Kelly Criterion (25% Kelly)

## Ports
- `5000` — Web dashboard (webview)
