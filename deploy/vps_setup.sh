#!/usr/bin/env bash
# One-shot setup for a fresh Ubuntu/Debian VPS (tested shape: 22.04 / 24.04).
#
#   scp -r . root@your-ip:/opt/market-desk      # or use deploy/sync.sh
#   ssh root@your-ip 'bash /opt/market-desk/deploy/vps_setup.sh'
#
# What it does: installs Docker, locks the firewall to SSH, refuses to start
# without a bot token, builds the image and starts one always-on container.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/market-desk}"
cd "$APP_DIR"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (or with sudo)." >&2
  exit 1
fi

say "1/5  Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl rsync ufw >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  say "2/5  Docker"
  curl -fsSL https://get.docker.com | sh
else
  say "2/5  Docker already present"
fi

# compose v2 plugin
if ! docker compose version >/dev/null 2>&1; then
  apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1 || \
    apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || true
fi
systemctl enable --now docker >/dev/null

say "3/5  Firewall (SSH only — nothing here listens on a port)"
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true

say "4/5  Secrets"
if [[ ! -f .env ]]; then
  cp .env.example .env
  read -r -p "Bot token from @BotFather: " TOKEN
  read -r -p "Your Telegram chat id (from @userinfobot): " CHAT
  read -r -p "Invite code for friends (blank = open to anyone): " CODE
  python3 - "$TOKEN" "$CHAT" "$CODE" <<'PY'
import sys, pathlib
tok, chat, code = sys.argv[1:4]
p = pathlib.Path('.env')
p.write_text(
    "TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\nTELEGRAM_INVITE_CODE=%s\n"
    % (tok.strip(), chat.strip(), code.strip()))
PY
  chmod 600 .env
  echo "wrote .env (mode 600)"
else
  echo ".env already exists — leaving it alone"
fi

if ! grep -q '^TELEGRAM_BOT_TOKEN=.\+' .env; then
  echo "TELEGRAM_BOT_TOKEN is empty in $APP_DIR/.env — refusing to start." >&2
  exit 1
fi

say "5/5  Start"
mkdir -p data/candles reports logs
docker compose up -d --build

sleep 6
docker compose ps
say "Logs (Ctrl-C to leave the stream; the service keeps running)"
echo "  docker compose logs -f --tail=40"
say "Health"
echo "  docker compose exec desk python tools/subscribers.py"
echo "  docker compose exec desk python tools/scheduler.py --dry-run"
echo
echo "Send /start ${CODE:-<no code>} to your bot to confirm delivery."
