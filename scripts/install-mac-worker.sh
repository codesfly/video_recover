#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ENV_FILE="$PROJECT_ROOT/.env"
SUPPORT_DIR="$HOME/Library/Application Support/VideoRecover"
VENV_DIR="$SUPPORT_DIR/worker-venv"
MODEL_DIR="$SUPPORT_DIR/models"
LOG_DIR="$HOME/Library/Logs/VideoRecover"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/com.codesfly.video-recover.worker.plist"
LABEL="com.codesfly.video-recover.worker"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "MLX Worker 只能安装在 macOS。" >&2
  exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "请先运行 ./scripts/dev-up.sh 生成本地配置。" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

if [[ -z "${VIDEO_RECOVER_WORKER_TOKEN:-}" || -z "${VIDEO_RECOVER_DATA_DIR:-}" ]]; then
  echo ".env 缺少 Worker Token 或数据目录。" >&2
  exit 1
fi

FFMPEG_BIN="$(command -v ffmpeg || true)"
if [[ -z "$FFMPEG_BIN" ]]; then
  echo "MLX Worker 需要 ffmpeg；请先运行：brew install ffmpeg" >&2
  exit 1
fi
WORKER_PATH="$(dirname "$FFMPEG_BIN"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3.12 || command -v python3)"
fi

mkdir -p "$SUPPORT_DIR" "$MODEL_DIR" "$LOG_DIR" "$(dirname "$LAUNCH_AGENT")"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install --upgrade "$PROJECT_ROOT[mac]"

python3 - \
  "$PROJECT_ROOT/deploy/com.codesfly.video-recover.worker.plist" \
  "$LAUNCH_AGENT" \
  "$VENV_DIR/bin/video-recover-mac-worker" \
  "$VIDEO_RECOVER_DATA_DIR" \
  "$VIDEO_RECOVER_WORKER_TOKEN" \
  "$MODEL_DIR" \
  "$SUPPORT_DIR" \
  "$WORKER_PATH" \
  "$LOG_DIR" <<'PY'
from __future__ import annotations

import plistlib
import sys
from pathlib import Path

template, destination, *values = sys.argv[1:]
replacements = dict(
    zip(
        (
            "__WORKER_EXECUTABLE__",
            "__DATA_DIR__",
            "__WORKER_TOKEN__",
            "__MODEL_DIR__",
            "__SUPPORT_DIR__",
            "__WORKER_PATH__",
            "__LOG_DIR__",
        ),
        values,
        strict=True,
    )
)
with Path(template).open("rb") as source:
    payload = plistlib.load(source)

def replace(value):
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [replace(item) for item in value]
    if isinstance(value, dict):
        return {key: replace(item) for key, item in value.items()}
    return value

target = Path(destination)
temporary = target.with_suffix(".tmp")
with temporary.open("wb") as output:
    plistlib.dump(replace(payload), output, sort_keys=False)
temporary.replace(target)
PY

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
BOOTSTRAPPED="false"
for attempt in {1..20}; do
  if launchctl bootstrap "$DOMAIN" "$LAUNCH_AGENT"; then
    BOOTSTRAPPED="true"
    break
  fi
  sleep 0.25
done
if [[ "$BOOTSTRAPPED" != "true" ]]; then
  echo "LaunchAgent 注册失败，请查看 launchd 日志。" >&2
  exit 1
fi
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "macOS MLX Worker 已安装并在后台运行。"
echo "日志：$LOG_DIR/worker.error.log"
