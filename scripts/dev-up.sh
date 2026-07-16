#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ENV_FILE="$PROJECT_ROOT/.env"
DATA_DIR="$PROJECT_ROOT/data"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$PROJECT_ROOT/.env.example" "$ENV_FILE"
fi

CURRENT_TOKEN="$(sed -n 's/^VIDEO_RECOVER_WORKER_TOKEN=//p' "$ENV_FILE" | tail -n 1 | tr -d '\"')"
if [[ -z "$CURRENT_TOKEN" || "$CURRENT_TOKEN" == "change-me" ]]; then
  CURRENT_TOKEN="$(openssl rand -hex 32)"
fi

python3 - "$ENV_FILE" "$DATA_DIR" "$CURRENT_TOKEN" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
data_dir = Path(sys.argv[2]).resolve()
token = sys.argv[3]
updates = {
    "VIDEO_RECOVER_DATA_DIR": json.dumps(str(data_dir)),
    "VIDEO_RECOVER_WORKER_TOKEN": token,
}
lines = env_path.read_text(encoding="utf-8").splitlines()
seen: set[str] = set()
output: list[str] = []
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else ""
    if key in updates:
        output.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        output.append(line)
for key, value in updates.items():
    if key not in seen:
        output.append(f"{key}={value}")
env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
PY

mkdir -p "$DATA_DIR/browser-capture"
cd "$PROJECT_ROOT"

echo "[1/3] 构建新镜像（当前服务保持运行）"
docker compose build
echo "[2/3] 启动或平滑替换本地服务"
docker compose up -d
echo "[3/3] 验证健康、Web 与 MCP"
"$PROJECT_ROOT/scripts/dev-check.sh"
