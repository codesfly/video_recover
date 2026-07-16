#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$PROJECT_ROOT"

for attempt in $(seq 1 40); do
  if curl --fail --silent --max-time 3 http://127.0.0.1:8787/healthz >/tmp/video-recover-health.json; then
    break
  fi
  if [[ "$attempt" == "40" ]]; then
    echo "服务在等待时间内没有就绪。" >&2
    docker compose ps
    docker compose logs --tail=80 app
    exit 1
  fi
  sleep 2
done

python3 - /tmp/video-recover-health.json <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
if payload != {"status": "ok", "storage": "ok"}:
    raise SystemExit(f"unexpected health response: {payload!r}")
PY

WEB_STATUS="$(curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1:8787/)"
if [[ "$WEB_STATUS" != "200" ]]; then
  echo "Web 管理台返回 HTTP $WEB_STATUS" >&2
  exit 1
fi

MCP_RESPONSE="$(curl --fail --silent --max-time 10 \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"health-check","version":"1"}}}' \
  http://127.0.0.1:8787/mcp)"

python3 - "$MCP_RESPONSE" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
name = payload.get("result", {}).get("serverInfo", {}).get("name")
if name != "video-recover":
    raise SystemExit(f"unexpected MCP response: {payload!r}")
PY

CONTAINER_ID="$(docker compose ps -q app)"
CONTAINER_HEALTH="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CONTAINER_ID")"
if [[ "$CONTAINER_HEALTH" != "healthy" && "$CONTAINER_HEALTH" != "running" ]]; then
  echo "容器状态异常：$CONTAINER_HEALTH" >&2
  exit 1
fi

if docker compose logs --no-color --tail=200 app | grep -Eqi 'traceback|panic|worker_token|sessionid='; then
  echo "容器日志中发现异常堆栈或疑似敏感字段。" >&2
  exit 1
fi

echo "VideoRecover 已就绪：http://127.0.0.1:8787"
echo "健康检查、Web、MCP 与容器状态均正常。"
