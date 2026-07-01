#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8000}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
SERVICE_NAME="${SERVICE_NAME:-}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_SERVICE="${INSTALL_SERVICE:-auto}"
PORT_SET_BY_ARG=0
ADMIN_PASSWORD_SET_BY_ARG=0
SERVICE_NAME_SET_BY_ARG=0
HAS_TTY=0
if [[ -r /dev/tty ]]; then
  HAS_TTY=1
fi

usage() {
  cat <<EOF
Usage: bash scripts/install.sh [--port 8000] [--admin-password PASSWORD] [--service-name NAME] [--no-service]

Options:
  --port             Service port, default 8000
  --admin-password   Admin password, required for first deployment
  --service-name     systemd service name, default baidu-openai-proxy-PORT
  --no-service       Do not install or update a systemd service
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; PORT_SET_BY_ARG=1; shift 2 ;;
    --admin-password) ADMIN_PASSWORD="$2"; ADMIN_PASSWORD_SET_BY_ARG=1; shift 2 ;;
    --service-name) SERVICE_NAME="$2"; SERVICE_NAME_SET_BY_ARG=1; shift 2 ;;
    --no-service) INSTALL_SERVICE="false"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

if [[ "$HAS_TTY" -eq 1 ]]; then
  if [[ "$PORT_SET_BY_ARG" -eq 0 ]]; then
    read -r -p "Project port [${PORT}]: " INPUT_PORT < /dev/tty
    PORT="${INPUT_PORT:-$PORT}"
  fi
  if [[ "$ADMIN_PASSWORD_SET_BY_ARG" -eq 0 && -z "$ADMIN_PASSWORD" ]]; then
    read -r -s -p "Admin password: " ADMIN_PASSWORD < /dev/tty
    echo
  fi
  if [[ "$INSTALL_SERVICE" == "auto" ]]; then
    read -r -p "Install and enable systemd service for auto-start? [Y/n]: " INPUT_SERVICE < /dev/tty
    case "${INPUT_SERVICE:-Y}" in
      n|N|no|NO|No) INSTALL_SERVICE="false" ;;
      *) INSTALL_SERVICE="true" ;;
    esac
  fi
fi

if [[ -z "$ADMIN_PASSWORD" ]]; then
  echo "ERROR: admin password is required. Run interactively or pass --admin-password PASSWORD."
  exit 1
fi

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [[ "$PORT" -lt 1 || "$PORT" -gt 65535 ]]; then
  echo "ERROR: --port must be an integer between 1 and 65535"
  exit 1
fi

if [[ -z "$SERVICE_NAME" ]]; then
  SERVICE_NAME="baidu-openai-proxy-${PORT}"
elif [[ "$SERVICE_NAME_SET_BY_ARG" -eq 0 ]]; then
  SERVICE_NAME="${SERVICE_NAME}-${PORT}"
fi

if ! [[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]]; then
  echo "ERROR: service name may only contain letters, numbers, underscore, dot, @ and dash"
  exit 1
fi

cd "$APP_DIR"
mkdir -p data logs

if [[ ! -d ".venv" ]]; then
  "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [[ ! -f ".env" ]]; then
  cp .env.example .env
fi

APP_SECRET_VALUE="$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"

INSTALL_PORT="$PORT" INSTALL_ADMIN_PASSWORD="$ADMIN_PASSWORD" INSTALL_APP_SECRET="$APP_SECRET_VALUE" python - <<'PY'
import os
from pathlib import Path
path = Path(".env")
text = path.read_text(encoding="utf-8")
updates = {
    "APP_PORT": os.environ["INSTALL_PORT"],
    "ADMIN_PASSWORD": os.environ["INSTALL_ADMIN_PASSWORD"],
    "APP_SECRET": os.environ["INSTALL_APP_SECRET"],
}
for key, value in updates.items():
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = key + "=" + value
            break
    else:
        lines.append(key + "=" + value)
    text = "\n".join(lines) + "\n"
path.write_text(text, encoding="utf-8")
PY

INSTALL_ADMIN_PASSWORD="$ADMIN_PASSWORD" python - <<'PY'
import os
from app.db.init_db import init_db

key = init_db(admin_password=os.environ["INSTALL_ADMIN_PASSWORD"])
if key:
    print(f"Created default API key: {key}")
else:
    print("Database initialized.")
PY

if [[ "$INSTALL_SERVICE" != "false" ]] && command -v systemctl >/dev/null 2>&1; then
  SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
  if [[ "$EUID" -ne 0 ]]; then
    echo "systemd setup requires root. Re-run with sudo to install service."
  else
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Baidu OpenAI Proxy (${PORT})
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"
    systemctl status "$SERVICE_NAME" --no-pager || true
  fi
else
  echo "Skipped systemd service setup."
fi

echo "Deployment completed."
echo "App directory: ${APP_DIR}"
echo "Service name: ${SERVICE_NAME}"
echo "Admin: http://SERVER_IP:${PORT}/admin"
