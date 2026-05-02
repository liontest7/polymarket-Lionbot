#!/bin/bash
# setup.sh — Linux/macOS installer for Polymarket Bot v2.0 Pro
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo "══════════════════════════════════════════════════════"
echo "   POLYMARKET BOT v2.0 PRO — Setup"
echo "══════════════════════════════════════════════════════"
echo ""

# Python check
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}ERROR: python3 not found. Install Python 3.11+${NC}"
    exit 1
fi
echo -e "${GREEN}✓ $(python3 --version)${NC}"

# .env
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo -e "${GREEN}✓ Created .env from template${NC}"
else
    echo "  .env already exists"
fi

# venv
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo -e "${GREEN}✓ Virtual environment created${NC}"
fi

# activate & install
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo -e "${GREEN}✓ Dependencies installed${NC}"

# dirs
mkdir -p logs
echo -e "${GREEN}✓ Directories ready${NC}"

echo ""
echo -e "${GREEN}Setup complete!${NC}"
echo ""
echo "  PAPER mode (demo):  python run.py"
echo "  LIVE mode:          python run.py --live"
echo "  Dashboard only:     python run.py --web-only"
echo ""
echo "  Dashboard at: http://localhost:8080"
echo ""
