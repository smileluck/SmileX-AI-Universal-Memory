#!/usr/bin/env bash
# SmileX Memory 常驻服务注册(Linux systemd --user)
# 用法: ./register-service-linux.sh [--uninstall]
# 可选: SMILEX_SERVE_ARGS="--config ~/.smilex/config.yaml --port 9000" ./register-service-linux.sh
set -euo pipefail

UNIT_NAME="smilex-memory.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT_PATH"
    systemctl --user daemon-reload
    echo "已卸载 $UNIT_NAME"
    exit 0
fi

EXE="$(command -v smilex-memory || true)"
if [[ -z "$EXE" ]]; then
    echo "未找到 smilex-memory,请先 pip install 'smilex-ai-memory[server]'" >&2
    exit 1
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=SmileX Agent Memory Server (MCP + panel)
After=default.target

[Service]
ExecStart=$EXE serve ${SMILEX_SERVE_ARGS:-}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"
echo "已注册并启动 $UNIT_NAME"
echo "管理: systemctl --user status|stop $UNIT_NAME; 本脚本 --uninstall 卸载"
echo "提示: loginctl enable-linger \$USER 可让用户未登录时也常驻"
