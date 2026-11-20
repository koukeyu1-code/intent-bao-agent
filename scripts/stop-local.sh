#!/usr/bin/env bash
# 意向宝 macOS 停止脚本（对应 Windows 的 scripts/stop-local.ps1）
# 用法：scripts/stop-local.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="${INTENT_BAO_RUNTIME_DIR:-$PROJECT_ROOT/runtime}"

ok()   { printf '\033[32m[意向宝]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[意向宝]\033[0m %s\n' "$*"; }

proc_name() { # $1=pid → 进程名（去路径），不存在输出空
  ps -p "$1" -o comm= 2>/dev/null | sed 's#.*/##' || true
}

# 名称:pid文件:meta文件:期望进程关键词
SERVICES=(
  "后端:backend.pid:backend.meta.json:python"
  "前端:frontend.pid:frontend.meta.json:node"
  "ASR语音:asr.pid:asr.meta.json:llama-server"
)

for entry in "${SERVICES[@]}"; do
  IFS=":" read -r label pid_file meta_file expected <<<"$entry"
  pid_path="$RUNTIME_DIR/$pid_file"
  meta_path="$RUNTIME_DIR/$meta_file"
  if [ ! -f "$pid_path" ]; then
    continue
  fi
  pid="$(tr -dc '0-9' < "$pid_path" 2>/dev/null || true)"
  name="$(proc_name "$pid" | tr '[:upper:]' '[:lower:]')"
  if [ -n "$pid" ] && printf '%s' "$name" | grep -q "$expected"; then
    kill "$pid" >/dev/null 2>&1 || true
    sleep 0.3
    kill -9 "$pid" >/dev/null 2>&1 || true
    rm -f "$pid_path" "$meta_path"
    ok "$label 服务已停止（pid $pid）。"
  else
    warn "$label 服务的 pid 记录无法通过可信校验（期望 $expected，实际 ${name:-无}），未停止任何进程。请人工核对后清理 $pid_path。"
  fi
done

ok "意向宝本地停止操作完成。"
