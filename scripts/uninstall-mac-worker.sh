#!/usr/bin/env bash
set -euo pipefail

SUPPORT_DIR="$HOME/Library/Application Support/VideoRecover"
VENV_DIR="$SUPPORT_DIR/worker-venv"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/com.codesfly.video-recover.worker.plist"
LABEL="com.codesfly.video-recover.worker"

if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "卸载 MLX Worker？下载视频和模型缓存会保留。[y/N] " answer
  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    echo "已取消。"
    exit 0
  fi
fi

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
rm -f "$LAUNCH_AGENT"
rm -rf "$VENV_DIR"

echo "MLX Worker 已卸载；馆藏与 $SUPPORT_DIR/models 均已保留。"
