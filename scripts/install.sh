#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8000}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
SERVICE_NAME="${SERVICE_NAME:-baidu-openai-proxy}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  cat <<EOF
Usage: bash scripts/install.sh [--port 8000] [--admin-password PASSWORD] [--service-name NAME]

Options:
  --port             Service port, default 8000
  --admin-password   Admin password, required for first deployment
  --service-name     systemd service name, default baidu-openai-proxy
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --admin-password) ADMIN_PASSWORD="$2"; shift 2 ;;
    --service-name) SERVICE_NAME="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

if [[ -z "$ADMIN_PASSWORD" ]]; then
  echo "ERROR: --admin-password is required"
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

python - <<PY
from pathlib import Path
path = Path(".env")
text = path.read_text(encoding="utf-8")
updates = {
    "APP_PORT": "$PORT",
    "ADMIN_PASSWORD": "$ADMIN_PASSWORD",
    "APP_SECRET": "$APP_SECRET_VALUE",
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

if command -v systemctl >/dev/null 2>&1; then
  SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
  if [[ "$EUID" -ne 0 ]]; then
    echo "systemd setup requires root. Re-run with sudo to install service."
  else
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Baidu OpenAI Proxy
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
fi

echo "Deployment completed."
echo "Admin: http://SERVER_IP:${PORT}/admin"
