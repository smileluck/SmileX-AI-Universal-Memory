#!/usr/bin/env bash
# SmileX Memory 常驻服务注册(macOS launchd)
# 用法: ./register-service-macos.sh [--uninstall]
set -euo pipefail

LABEL="com.smilex.memory"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "已卸载 $LABEL"
    exit 0
fi

EXE="$(command -v smilex-memory || true)"
if [[ -z "$EXE" ]]; then
    echo "未找到 smilex-memory,请先 pip install 'smilex-ai-memory[server]'" >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$EXE</string><string>serve</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$HOME/.smilex/serve.log</string>
    <key>StandardErrorPath</key><string>$HOME/.smilex/serve.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "已注册并启动 $LABEL"
echo "管理: launchctl stop|start $LABEL; 本脚本 --uninstall 卸载"
