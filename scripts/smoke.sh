#!/usr/bin/env bash
# 端到端冒烟：提交一次作业，轮询到终态，断言状态与结果形状（spec/040 TC-S-001 的脚本版）。
#
#   BASE_URL=http://127.0.0.1:8012 scripts/smoke.sh
#   退出码：0 = COMPLETE；1 = ERROR / 超时 / 服务不可达
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8012}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-600}"
POLL_INTERVAL="${POLL_INTERVAL:-3}"

DOMAIN="${SMOKE_DOMAIN:-openai.com}"
DESCRIPTION="${SMOKE_DESCRIPTION:-为一家 AI 基础设施公司制定新品发布营销方案}"

log() { printf '[smoke] %s\n' "$*" >&2; }
die() { printf '[smoke] 失败：%s\n' "$*" >&2; exit 1; }

json_get() {
  # $1 = JSON 文本，$2 = 点分路径（如 status / result.strategy.name）
  python3 -c '
import json, sys
data = json.loads(sys.argv[1])
for part in sys.argv[2].split("."):
    if data is None:
        break
    if isinstance(data, dict):
        data = data.get(part)
    else:
        data = None
print("" if data is None else data)
' "$1" "$2"
}

# ---------------------------------------------------------------- 等待服务就绪
log "等待服务就绪：${BASE_URL}/healthz"
for _ in $(seq 1 30); do
  if curl -fsS "${BASE_URL}/healthz" >/dev/null 2>&1; then
    log "服务已就绪"
    break
  fi
  sleep 1
done
curl -fsS "${BASE_URL}/healthz" >/dev/null 2>&1 || die "服务不可达：${BASE_URL}"

# ---------------------------------------------------------------- 提交作业
payload=$(python3 -c '
import json, sys
print(json.dumps({"customer_domain": sys.argv[1], "project_description": sys.argv[2]}, ensure_ascii=False))
' "${DOMAIN}" "${DESCRIPTION}")

response=$(curl -fsS -X POST "${BASE_URL}/api/crew" \
  -H 'Content-Type: application/json' -d "${payload}") || die "提交作业失败"

job_id=$(json_get "${response}" job_id)
[ -n "${job_id}" ] || die "响应里没有 job_id：${response}"
log "已提交 job_id=${job_id}"

# ---------------------------------------------------------------- 轮询到终态
deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
status=""
while :; do
  body=$(curl -fsS "${BASE_URL}/api/crew/${job_id}") || die "查询作业失败"
  status=$(json_get "${body}" status)
  log "status=${status}"

  case "${status}" in
    COMPLETE) break ;;
    ERROR)    die "作业失败：$(json_get "${body}" result)" ;;
  esac

  if [ "$(date +%s)" -ge "${deadline}" ]; then
    die "等待 ${TIMEOUT_SECONDS}s 仍未到终态（最后状态 ${status}）"
  fi
  sleep "${POLL_INTERVAL}"
done

# ---------------------------------------------------------------- 断言结果形状
for key in research_report strategy campaign_idea copy; do
  value=$(json_get "${body}" "result.${key}")
  [ -n "${value}" ] || die "结果缺少字段：${key}"
done

events=$(python3 -c '
import json, sys
print(len(json.loads(sys.argv[1]).get("events", [])))
' "${body}")
[ "${events}" -gt 0 ] || die "事件流水为空"

cat <<EOF
[smoke] 通过 ✅
  job_id       : ${job_id}
  status       : ${status}
  events       : ${events} 条
  strategy.name: $(json_get "${body}" result.strategy.name)
  copy.title   : $(json_get "${body}" result.copy.title)
EOF
