"""
src/api/server.py
FastAPI web server — REST + WebSocket + Bot control.
Provides /api/bot/start and /api/bot/stop for dashboard control.
"""

import asyncio
import json
import os
import time
from typing import Optional, Dict, Any
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from src.models import Trade, PolyWindow, AssetPrice
from src.risk.manager import RiskManager
from config.settings import Settings

BTCPrice = AssetPrice


class BotState:
    def __init__(self):
        self.btc_price: Optional[float] = None
        self.btc_delta: Optional[float] = None
        self.window: Optional[dict] = None
        self.active_markets: dict = {}
        self.mode: str = "paper"
        self.capital: float = 0.0
        self.daily_pnl: float = 0.0
        self.all_time_pnl: float = 0.0
        self.win_rate: float = 0.0
        self.total_trades: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.trades_today: int = 0
        self.max_trades_today: int = 20
        self.fees_paid: float = 0.0
        self.peak_capital: float = 0.0
        self.trades: list[dict] = []
        self.bot_running: bool = False
        self.last_signal: Optional[str] = None
        self.last_update: float = time.time()

    def to_dict(self) -> dict:
        return {
            "btc_price": self.btc_price,
            "btc_delta": self.btc_delta,
            "window": self.window,
            "active_markets": self.active_markets,
            "mode": self.mode,
            "capital": self.capital,
            "daily_pnl": self.daily_pnl,
            "all_time_pnl": self.all_time_pnl,
            "win_rate": self.win_rate,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "trades_today": self.trades_today,
            "max_trades_today": self.max_trades_today,
            "fees_paid": self.fees_paid,
            "peak_capital": self.peak_capital,
            "trades": self.trades[-50:],
            "bot_running": self.bot_running,
            "last_signal": self.last_signal,
            "last_update": self.last_update,
        }


bot_state = BotState()

app = FastAPI(title="Polymarket Bot API", version="4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        msg = json.dumps(data)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()

ENV_PATH = Path(__file__).parent.parent.parent / ".env"

_bot_task: Optional[asyncio.Task] = None
_bot_settings: Optional[Settings] = None


def _read_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _write_env(data: dict) -> None:
    example_path = ENV_PATH.parent / ".env.example"
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    elif example_path.exists():
        lines = example_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    result = []
    written = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            result.append(line)
            continue
        if "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in data:
                result.append(f"{k}={data[k]}")
                written.add(k)
            else:
                result.append(line)

    for k, v in data.items():
        if k not in written:
            result.append(f"{k}={v}")

    ENV_PATH.write_text("\n".join(result) + "\n", encoding="utf-8")


@app.get("/api/state")
async def get_state():
    return JSONResponse(bot_state.to_dict())


@app.get("/api/trades")
async def get_trades():
    return JSONResponse({"trades": bot_state.trades})


@app.get("/api/health")
async def health():
    return {"status": "ok", "bot_running": bot_state.bot_running}


@app.get("/api/settings")
async def get_settings():
    env = _read_env()
    for k in ["POLYMARKET_PK", "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET",
              "POLYMARKET_API_PASSPHRASE", "ALCHEMY_API_KEY"]:
        if env.get(k) and env[k] not in ("", "0x0", "your-api-key-here",
                "your-api-secret-here", "your-passphrase-here",
                "your-alchemy-key-here", "0xYOUR_PRIVATE_KEY_HERE"):
            env[k] = env[k][:6] + "..." + env[k][-4:] if len(env[k]) > 12 else "****"
    return JSONResponse(env)


@app.get("/api/settings/raw")
async def get_settings_raw():
    return JSONResponse(_read_env())


@app.post("/api/settings")
async def save_settings(request: Request):
    try:
        data = await request.json()
        allowed = {
            "TRADING_MODE", "CAPITAL_USD", "MAX_POSITION_PCT",
            "DAILY_LOSS_LIMIT_PCT", "MAX_TRADES_PER_DAY",
            "MIN_BTC_DELTA_PCT", "MIN_EDGE_AFTER_FEES", "ENTRY_WINDOW_SECONDS",
            "TAKER_FEE_PCT", "MAKER_FEE_PCT", "LOG_LEVEL",
            "POLYMARKET_PK", "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET",
            "POLYMARKET_API_PASSPHRASE", "ALCHEMY_API_KEY"
        }
        filtered = {k: str(v) for k, v in data.items() if k in allowed}
        _write_env(filtered)
        return JSONResponse({"ok": True, "message": "Settings saved. Restart bot to apply."})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=400)


@app.post("/api/bot/start")
async def bot_start():
    global _bot_task, _bot_settings
    if _bot_task and not _bot_task.done():
        return JSONResponse({"ok": False, "message": "Bot already running"})

    try:
        from config.settings import Settings
        s = Settings()
        _bot_settings = s

        from src.bot import PolyBot
        bot = PolyBot(s)

        _bot_task = asyncio.create_task(bot.run(), name="bot_main")
        bot_state.bot_running = True
        await broadcast_state()
        logger.info("Bot started via API")
        return JSONResponse({"ok": True, "message": "Bot started"})
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.post("/api/bot/stop")
async def bot_stop():
    global _bot_task
    if _bot_task is None or _bot_task.done():
        bot_state.bot_running = False
        await broadcast_state()
        return JSONResponse({"ok": False, "message": "Bot not running"})
    _bot_task.cancel()
    try:
        await asyncio.wait_for(_bot_task, timeout=5.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    _bot_task = None
    bot_state.bot_running = False
    await broadcast_state()
    logger.info("Bot stopped via API")
    return JSONResponse({"ok": True, "message": "Bot stopped"})


@app.get("/api/bot/status")
async def bot_status():
    running = _bot_task is not None and not _bot_task.done()
    return JSONResponse({"running": running})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    await websocket.send_text(json.dumps(bot_state.to_dict()))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


async def broadcast_state():
    await manager.broadcast(bot_state.to_dict())


def update_from_bot(
    risk: RiskManager,
    settings: Settings,
    btc: Optional[AssetPrice] = None,
    window: Optional[PolyWindow] = None,
    windows: Optional[Dict[str, PolyWindow]] = None,
    delta: Optional[float] = None,
    new_trade: Optional[Trade] = None,
    running: bool = True,
    last_signal: Optional[str] = None,
):
    bot_state.bot_running = running
    bot_state.mode = settings.trading_mode

    if btc:
        bot_state.btc_price = btc.price
    if delta is not None:
        bot_state.btc_delta = delta
    if last_signal:
        bot_state.last_signal = last_signal

    if windows is not None:
        bot_state.active_markets = {
            asset: {
                "slug": w.slug,
                "seconds_remaining": w.seconds_remaining,
                "close_ts": w.close_ts,
                "open_price": w.open_price,
                "asset": w.asset,
            }
            for asset, w in windows.items()
            if w.is_active
        }
        if windows.get("BTC"):
            w = windows["BTC"]
            bot_state.window = {
                "slug": w.slug,
                "seconds_remaining": w.seconds_remaining,
                "close_ts": w.close_ts,
                "open_price": w.open_price,
            }
    elif window:
        bot_state.window = {
            "slug": window.slug,
            "seconds_remaining": window.seconds_remaining,
            "close_ts": window.close_ts,
            "open_price": window.open_price,
        }

    stats = risk.get_all_time_stats()
    daily = risk.get_daily_stats()
    bot_state.capital = stats["current_capital"]
    bot_state.peak_capital = stats["peak_capital"]
    bot_state.all_time_pnl = stats["total_pnl"]
    bot_state.win_rate = stats["win_rate"]
    bot_state.total_trades = stats["total_trades"]
    bot_state.wins = stats["wins"]
    bot_state.losses = stats["losses"]
    bot_state.fees_paid = stats["total_fees"]
    bot_state.daily_pnl = daily.net_pnl
    bot_state.trades_today = daily.trades
    bot_state.max_trades_today = settings.max_trades_per_day

    if new_trade:
        trade_dict = {
            "id": new_trade.id,
            "asset": new_trade.asset,
            "side": new_trade.side,
            "entry_price": new_trade.entry_price,
            "size_usd": new_trade.size_usd,
            "shares": new_trade.shares,
            "result": new_trade.result,
            "pnl_usd": new_trade.pnl_usd,
            "fees_paid": new_trade.fees_paid,
            "entry_time": new_trade.entry_time,
            "mode": new_trade.mode,
            "notes": new_trade.notes,
        }
        existing = next((t for t in bot_state.trades if t["id"] == new_trade.id), None)
        if existing:
            existing.update(trade_dict)
        else:
            bot_state.trades.append(trade_dict)

    bot_state.last_update = time.time()
