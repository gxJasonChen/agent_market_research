#!/usr/bin/env bash
# 本地拉起三件套：Celery Worker + FastAPI。MySQL / Redis 假定已在跑。
#
#   scripts/dev_up.sh          启动（后台，日志写到 .artifacts/）
#   scripts/dev_up.sh status   查看状态
#   scripts/dev_up.sh stop     停止
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

ARTIFACTS="${ROOT}/.artifacts"
PY="${ROOT}/.venv/bin/python"
PORT="${AMR_API_PORT:-8012}"

mkdir -p "${ARTIFACTS}"

wait_for_tcp() {
  local host="$1" port="$2" name="$3" tries=30
  for _ in $(seq 1 "${tries}"); do
    if "${PY}" -c "
import socket, sys
s = socket.socket()
s.settimeout(1)
sys.exit(0 if s.connect_ex(('${host}', ${port})) == 0 else 1)
" 2>/dev/null; then
      echo "[dev_up] ${name} 已就绪（${host}:${port}）"
      return 0
    fi
    sleep 1
  done
  echo "[dev_up] ${name} 不可达（${host}:${port}）" >&2
  return 1
}

start() {
  wait_for_tcp 127.0.0.1 3306 MySQL || exit 1
  wait_for_tcp 127.0.0.1 6379 Redis || exit 1

  echo "[dev_up] 启动 Celery Worker"
  ( cd "${ROOT}/src" && "${PY}" -m celery -A amr.worker.tasks:celery_app worker \
      --loglevel=info --concurrency=2 \
      >"${ARTIFACTS}/worker.log" 2>&1 & echo $! >"${ARTIFACTS}/worker.pid" )

  echo "[dev_up] 启动 FastAPI（:${PORT}）"
  "${PY}" -m uvicorn amr.api.app:app --host 0.0.0.0 --port "${PORT}" --app-dir "${ROOT}/src" \
    >"${ARTIFACTS}/api.log" 2>&1 &
  echo $! >"${ARTIFACTS}/api.pid"

  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
      echo "[dev_up] 服务已就绪：http://127.0.0.1:${PORT}  （文档 /docs）"
      echo "[dev_up] 日志：${ARTIFACTS}/api.log  ${ARTIFACTS}/worker.log"
      return 0
    fi
    sleep 1
  done
  echo "[dev_up] API 启动超时，请看 ${ARTIFACTS}/api.log" >&2
  exit 1
}

stop() {
  for name in api worker; do
    if [ -f "${ARTIFACTS}/${name}.pid" ]; then
      pid="$(cat "${ARTIFACTS}/${name}.pid")"
      if kill "${pid}" 2>/dev/null; then
        echo "[dev_up] 已停止 ${name}（pid=${pid}）"
      fi
      rm -f "${ARTIFACTS}/${name}.pid"
    fi
  done
}

status() {
  for name in api worker; do
    if [ -f "${ARTIFACTS}/${name}.pid" ] && kill -0 "$(cat "${ARTIFACTS}/${name}.pid")" 2>/dev/null; then
      echo "[dev_up] ${name} 运行中（pid=$(cat "${ARTIFACTS}/${name}.pid")）"
    else
      echo "[dev_up] ${name} 未运行"
    fi
  done
  curl -fsS "http://127.0.0.1:${PORT}/healthz" && echo || echo "[dev_up] API 不可达"
}

case "${1:-start}" in
  start)  start ;;
  stop)   stop ;;
  status) status ;;
  *) echo "用法：$0 [start|stop|status]" >&2; exit 2 ;;
esac
