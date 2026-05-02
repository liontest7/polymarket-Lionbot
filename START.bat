@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist "venv\Scripts\activate.bat" goto :DO_SETUP

:MENU
cls
echo.
echo  ===================================================
echo    POLYMARKET BOT v3  --  MAIN MENU
echo  ===================================================
echo.

set "MODE=paper"
if exist ".env" (
    for /f "usebackq tokens=1,2 delims==" %%A in (".env") do (
        if /i "%%A"=="TRADING_MODE" set "MODE=%%B"
    )
)

if /i "!MODE!"=="live" (
    echo    Current Mode: LIVE  ^(real money^)
) else (
    echo    Current Mode: PAPER  ^(demo - no real money^)
)
echo.
echo    [1]  Start Bot
echo    [2]  Open Dashboard in browser
echo    [3]  View recent logs
echo    [4]  Run setup / reinstall
echo    [0]  Exit
echo.
set /p "CHOICE=  Choose [0-4]: "

if "!CHOICE!"=="1" goto :START_BOT
if "!CHOICE!"=="2" goto :OPEN_BROWSER
if "!CHOICE!"=="3" goto :VIEW_LOGS
if "!CHOICE!"=="4" goto :DO_SETUP
if "!CHOICE!"=="0" exit /b 0
goto :MENU


:START_BOT
cls

if /i "!MODE!"=="live" (
    echo.
    echo  ===================================================
    echo    WARNING: LIVE MODE -- REAL MONEY ACTIVE
    echo  ===================================================
    echo.
    echo  The bot will place REAL orders on Polymarket.
    echo  Make sure API keys are set in the dashboard Settings tab.
    echo.
    set /p "CONFIRM=  Type YES to confirm: "
    if /i not "!CONFIRM!"=="YES" goto :MENU
    echo.
)

echo  Starting bot...
echo  Dashboard: http://localhost:8080
echo  Settings are in the dashboard Settings tab.
echo  Press Ctrl+C to stop.
echo.

call venv\Scripts\activate.bat

if /i "!MODE!"=="live" (
    python run.py --live
) else (
    python run.py
)

echo.
echo  Bot stopped.
pause
goto :MENU


:OPEN_BROWSER
start "" "http://localhost:8080"
timeout /t 2 /nobreak >nul
goto :MENU


:VIEW_LOGS
cls
echo  Recent log (last 60 lines):
echo.
if exist "logs\bot.log" (
    powershell -Command "Get-Content 'logs\bot.log' -Tail 60"
) else (
    echo  No log file yet. Run the bot first.
)
echo.
pause
goto :MENU


:DO_SETUP
cls
echo.
echo  ===================================================
echo    POLYMARKET BOT v3  --  SETUP
echo  ===================================================
echo.

echo  [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: Python not found!
    echo  Download from: https://www.python.org/downloads/
    echo  Check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%V in ('python --version 2^>^&1') do echo  OK: %%V
echo.

echo  [2/4] Setting up .env config...
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo  Created .env from template.
    ) else (
        echo  Creating default .env...
        (
            echo TRADING_MODE=paper
            echo CAPITAL_USD=100
            echo MAX_POSITION_PCT=0.10
            echo DAILY_LOSS_LIMIT_PCT=0.05
            echo MAX_TRADES_PER_DAY=20
            echo MIN_BTC_DELTA_PCT=0.05
            echo MIN_EDGE_AFTER_FEES=0.03
            echo ENTRY_WINDOW_SECONDS=25
            echo TAKER_FEE_PCT=0.0156
            echo MAKER_FEE_PCT=0.0
            echo LOG_LEVEL=INFO
            echo LOG_FILE=logs/bot.log
            echo POLYMARKET_PK=0x0
            echo POLYMARKET_API_KEY=
            echo POLYMARKET_API_SECRET=
            echo POLYMARKET_API_PASSPHRASE=
            echo ALCHEMY_API_KEY=
        ) > .env
    )
) else (
    echo  .env already exists, skipping.
)
echo.

echo  [3/4] Creating virtual environment...
if not exist "venv" (
    python -m venv venv
    if errorlevel 1 (
        echo  ERROR: Could not create virtual environment.
        pause
        exit /b 1
    )
    echo  Virtual environment created.
) else (
    echo  Virtual environment already exists.
)
echo.

echo  [4/4] Installing packages (may take 1-3 minutes)...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
pip install -r requirements.txt -q
if errorlevel 1 (
    echo.
    echo  Some packages failed. Retrying with output...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo  ERROR: Package install failed. Check internet connection.
        pause
        exit /b 1
    )
)
echo  All packages installed.
echo.

if not exist "logs" mkdir logs
if not exist "web\static\css" mkdir "web\static\css"
if not exist "web\static\js" mkdir "web\static\js"

echo  ===================================================
echo    SETUP COMPLETE!
echo    Press any key to return to menu, then choose [1].
echo  ===================================================
echo.
pause
goto :MENU
