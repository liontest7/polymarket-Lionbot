# Polymarket Bot v2.0 Pro

A professional trading bot for Polymarket's 5-minute BTC Up/Down markets.
Features a real-time web dashboard, paper trading mode, and live execution.

---

## Quick Start (Windows)

1. **Double-click `scripts/SETUP.bat`** — installs everything automatically
2. **Double-click `START_HERE.bat`** — main menu
3. Choose option `[1] Paper mode` — dashboard opens at **http://localhost:8080**

That's it. No API keys needed for paper mode.

---

## Quick Start (Linux / macOS)

```bash
chmod +x setup.sh
./setup.sh
source venv/bin/activate
python run.py          # paper mode
python run.py --live   # live mode
```

---

## Files Overview

```
polymarket-pro/
│
├── START_HERE.bat          ← Windows main menu (start here)
├── RUN_PAPER.bat           ← Quick launch: paper mode
├── RUN_LIVE.bat            ← Quick launch: live mode (with safety checks)
├── EDIT_SETTINGS.bat       ← Open .env in Notepad
├── VIEW_LOGS.bat           ← Tail log file
│
├── scripts/
│   └── SETUP.bat           ← Full installer
│
├── run.py                  ← Python entry point
├── .env                    ← Your config (never share this!)
├── .env.example            ← Template
├── requirements.txt        ← Python dependencies
│
├── src/
│   ├── bot.py              ← Main orchestrator
│   ├── models.py           ← Data models
│   ├── data/
│   │   ├── binance_feed.py ← Real-time BTC prices (WebSocket)
│   │   └── polymarket_feed.py ← Active market discovery
│   ├── signal/
│   │   └── engine.py       ← Signal calculation logic
│   ├── risk/
│   │   └── manager.py      ← Position sizing & limits
│   ├── execution/
│   │   └── executor.py     ← Paper & live order execution
│   ├── monitor/
│   │   └── dashboard.py    ← Terminal display (fallback)
│   └── api/
│       └── server.py       ← Web API (FastAPI + WebSocket)
│
├── web/
│   └── templates/
│       └── dashboard.html  ← Web dashboard UI
│
├── config/
│   ├── settings.py         ← Settings model (pydantic)
│   └── logger.py           ← Logging setup
│
└── logs/
    └── bot.log             ← Rolling log file
```

---

## Dashboard

The web dashboard at **http://localhost:8080** shows:

- **Bitcoin price** with real-time updates and 20-second delta
- **Active window** countdown with progress bar
- **Signal status** — when a signal fires
- **Capital & P&L** — current, daily, all-time
- **Win rate** with visual bar
- **Trade history** — last 30 trades with results
- **P&L curve** — cumulative PnL sparkline
- **Bot status** — running / stopped

---

## Configuration (`.env`)

| Setting | Default | Description |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` = demo, `live` = real money |
| `CAPITAL_USD` | `100` | Total capital to manage |
| `MAX_POSITION_PCT` | `0.10` | Max % of capital per trade |
| `DAILY_LOSS_LIMIT_PCT` | `0.05` | Stop trading at 5% daily loss |
| `MAX_TRADES_PER_DAY` | `20` | Hard cap on daily trades |
| `MIN_BTC_DELTA_PCT` | `0.05` | Min BTC move % to trigger signal |
| `MIN_EDGE_AFTER_FEES` | `0.03` | Min edge (3%) to place order |
| `ENTRY_WINDOW_SECONDS` | `25` | Trade in last N seconds of window |
| `TAKER_FEE_PCT` | `0.0156` | Polymarket taker fee (2026) |

---

## Live Mode Setup

> ⚠️ Only switch to live mode after at least 200 paper trades with positive net PnL.

1. Get API keys from **polymarket.com → Profile → API Keys**
2. Get a free Polygon RPC from **alchemy.com**
3. Edit `.env`:
   ```
   POLYMARKET_PK=0xYOUR_PRIVATE_KEY
   POLYMARKET_API_KEY=...
   POLYMARKET_API_SECRET=...
   POLYMARKET_API_PASSPHRASE=...
   ALCHEMY_API_KEY=...
   TRADING_MODE=live
   ```
4. Run `RUN_LIVE.bat` (requires typing "CONFIRM" + "LIVE")

---

## Strategy Summary

The bot monitors Binance for sharp BTC price moves in the last 20 seconds
of each 5-minute window. When BTC moves strongly (>0.05%) near window close,
there is often a lag between Binance and Polymarket's Chainlink oracle.

The signal engine:
1. Calculates BTC delta in last 20 seconds
2. Maps delta to estimated win probability (calibrated table)
3. Checks expected edge = P(win) × $1 − token_price − fees
4. Only trades when edge > 3%

Risk management:
- Kelly Criterion (25% fraction) for position sizing
- 5% daily loss limit
- 25% total drawdown stop
- 20 trades/day maximum

---

## Important Disclaimers

- **Past performance does not guarantee future results.**
- The fee calibration and win probability table are estimates — validate with paper trading.
- 5-minute BTC markets on Polymarket are competitive — professional bots exist.
- Start with small capital. Never risk money you cannot afford to lose.
- This software is provided as-is, without warranty.
