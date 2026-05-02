"""
run.py — Entry point.
Starts FastAPI web server. Bot is started/stopped via the dashboard.
Browser opens automatically at http://localhost:8080
"""

import asyncio
import sys
import threading
import time
from pathlib import Path


def main():
    args = sys.argv[1:]
    live_mode = "--live" in args
    auto_start = "--no-autostart" not in args

    from config.logger import setup_logger
    from config.settings import Settings

    import os
    if live_mode:
        os.environ["TRADING_MODE"] = "live"

    s = Settings()
    setup_logger(s.log_level, s.log_file)

    mode_label = "LIVE (real money)" if s.trading_mode == "live" else "PAPER (demo)"
    print("")
    print("  POLYMARKET BOT v4  —  Multi-Asset Edition")
    print(f"  Mode:    {mode_label}")
    print(f"  Capital: ${s.capital_usd}")
    print(f"  Web UI:  http://localhost:8080")
    print("")
    print("  Dashboard running — use the Start button to begin trading")
    print("  Press Ctrl+C to quit.")
    print("")

    from src.api.server import app
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    html_path = Path(__file__).parent / "web" / "templates" / "dashboard.html"
    static_path = Path(__file__).parent / "web" / "static"

    if static_path.exists():
        try:
            app.mount("/static", StaticFiles(directory=str(static_path)), name="static")
        except Exception:
            pass

    @app.get("/", response_class=HTMLResponse)
    async def serve_dashboard():
        if html_path.exists():
            return HTMLResponse(html_path.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<h1 style='font-family:monospace;color:#0f0;background:#000;padding:40px'>"
            "Dashboard file missing.<br>Check web/templates/dashboard.html</h1>"
        )

    server_ready = threading.Event()

    def start_web():
        import uvicorn

        class NotifyServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets)
                server_ready.set()

        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=5000,
            log_level="warning",
            access_log=False,
        )
        NotifyServer(config).run()

    web_thread = threading.Thread(target=start_web, daemon=True)
    web_thread.start()

    ready = server_ready.wait(timeout=15)
    if not ready:
        print("  Warning: web server may not be ready yet")

    if auto_start:
        time.sleep(1.0)
        import requests
        try:
            r = requests.post("http://localhost:5000/api/bot/start", timeout=5)
            if r.ok and r.json().get("ok"):
                print("  Bot started automatically (use Stop button to pause)")
            else:
                print(f"  Auto-start note: {r.json().get('message','')}")
        except Exception as e:
            print(f"  Auto-start failed: {e} — use the Start button in the dashboard")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        import requests
        try:
            requests.post("http://localhost:5000/api/bot/stop", timeout=3)
        except Exception:
            pass
        print("  Stopped.")


if __name__ == "__main__":
    main()
