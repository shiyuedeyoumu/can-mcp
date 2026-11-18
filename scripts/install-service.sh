#!/usr/bin/env bash
# can-mcp systemd 安装/卸载脚本（幂等）
# 用法: ./install-service.sh [install|uninstall|status]
set -euo pipefail

SERVICE=can-mcp.service
UNIT=/etc/systemd/system/${SERVICE}
PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
RUN_USER=${RUN_USER:-$(id -un)}
RUN_DIR=/opt/can-mcp
CMD=${1:-install}
BUILD_ID=$(git -C "$PROJECT_DIR" rev-parse --short=12 HEAD 2>/dev/null || printf "unknown")

case "$CMD" in
  uninstall)
    sudo systemctl disable --now "$SERVICE" 2>/dev/null || true
    sudo rm -f "$UNIT"
    sudo systemctl daemon-reload
    echo "$SERVICE removed"
    ;;
  status)
    systemctl --no-pager --full status "$SERVICE" || true
    ;;
  install)
    [ -x "$PROJECT_DIR/venv/bin/can-mcp" ] || { echo "ERROR: $PROJECT_DIR/venv/bin/can-mcp missing"; exit 1; }
    # 用 /opt 下的软链做运行目录，避开中文路径在 unit 里的编码问题
    sudo ln -sfn "$PROJECT_DIR" "$RUN_DIR"
    sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=CAN MCP Server (python-can / PEAK PCAN)
Documentation=file://$RUN_DIR/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$RUN_DIR
Environment=HOME=/home/$RUN_USER
Environment=PYTHONUNBUFFERED=1
Environment=CAN_MCP_BUILD=$BUILD_ID
ExecStart=$RUN_DIR/venv/bin/can-mcp
Restart=on-failure
RestartSec=3
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$SERVICE"
    sleep 1
    systemctl --no-pager --full status "$SERVICE" || true
    ;;
  *)
    echo "usage: $0 [install|uninstall|status]"; exit 2
    ;;
esac
