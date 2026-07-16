#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
COMPOSE_FILE="$PROJECT_ROOT/compose.yaml"
CLAUDE_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
DOCKER_BIN="$(command -v docker || true)"
APPLY=false

if [[ "${1:-}" == "--apply" ]]; then
  APPLY=true
fi

echo "Codex Streamable HTTP MCP："
echo "codex mcp add video-recover --url http://127.0.0.1:8787/mcp"
echo
echo "Claude Desktop stdio 使用：docker compose exec -T app python -m video_recover.mcp_stdio"

if [[ "$APPLY" != "true" ]]; then
  echo
  echo "当前为预览模式；加 --apply 后写入 Codex 与 Claude Desktop 配置。"
  exit 0
fi

if [[ -z "$DOCKER_BIN" ]]; then
  echo "找不到 docker 命令。" >&2
  exit 1
fi

if command -v codex >/dev/null 2>&1; then
  codex mcp remove video-recover >/dev/null 2>&1 || true
  codex mcp add video-recover --url http://127.0.0.1:8787/mcp
else
  echo "未找到 codex 命令，已跳过 Codex 配置。" >&2
fi

mkdir -p "$(dirname "$CLAUDE_CONFIG")"
if [[ -f "$CLAUDE_CONFIG" ]]; then
  backup="$CLAUDE_CONFIG.backup.$(date +%Y%m%d-%H%M%S)"
  cp "$CLAUDE_CONFIG" "$backup"
  echo "Claude Desktop 原配置已备份：$backup"
fi

python3 - "$CLAUDE_CONFIG" "$DOCKER_BIN" "$PROJECT_ROOT" "$COMPOSE_FILE" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
docker_bin, project_root, compose_file = sys.argv[2:]
try:
    config = json.loads(config_path.read_text(encoding="utf-8"))
except FileNotFoundError:
    config = {}
except json.JSONDecodeError as exc:
    raise SystemExit(f"Claude Desktop 配置不是有效 JSON：{exc}") from exc

servers = config.setdefault("mcpServers", {})
servers["video-recover"] = {
    "command": docker_bin,
    "args": [
        "compose",
        "--project-directory",
        project_root,
        "-f",
        compose_file,
        "exec",
        "-T",
        "app",
        "python",
        "-m",
        "video_recover.mcp_stdio",
    ],
}
temporary = config_path.with_suffix(".tmp")
temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
temporary.replace(config_path)
PY

echo "MCP 已配置。请重启 Claude Desktop 使 stdio 服务生效。"
