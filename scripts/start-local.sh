#!/usr/bin/env bash
# 意向宝 macOS 启动脚本（对应 Windows 的 scripts/start-local.ps1）
# 启动三个本地服务：ASR 语音(llama-server:8099) + 后端(FastAPI:8000) + 前端(vinext:3000)
# 用法：scripts/start-local.sh [--no-browser] [--backend-port N] [--frontend-port N] [--asr-port N]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_ROOT="$PROJECT_ROOT/server"
RUNTIME_DIR="${INTENT_BAO_RUNTIME_DIR:-$PROJECT_ROOT/runtime}"
DATA_DIR="${INTENT_BAO_DATA_DIR:-$BACKEND_ROOT/data}"
VENV_PYTHON="$BACKEND_ROOT/.venv/bin/python"
FRONTEND_CLI="$PROJECT_ROOT/node_modules/vinext/dist/cli.js"

info()  { printf '\033[36m[意向宝]\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m[意向宝] 警告：\033[0m %s\n' "$*"; }
ok()    { printf '\033[32m[意向宝]\033[0m %s\n' "$*"; }
fail()  { printf '\033[31m[意向宝] 启动失败：\033[0m %s\n' "$*" >&2; exit 1; }

BACKEND_PORT=8000
FRONTEND_PORT=3000
ASR_PORT=8099
NO_BROWSER=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-browser)    NO_BROWSER=1; shift ;;
    --backend-port)  BACKEND_PORT="${2:?缺少端口值}"; shift 2 ;;
    --frontend-port) FRONTEND_PORT="${2:?缺少端口值}"; shift 2 ;;
    --asr-port)      ASR_PORT="${2:?缺少端口值}"; shift 2 ;;
    *) fail "未知参数：$1（支持 --no-browser / --backend-port / --frontend-port / --asr-port）" ;;
  esac
done

BACKEND_URL="http://127.0.0.1:$BACKEND_PORT/api/health"
FRONTEND_URL="http://127.0.0.1:$FRONTEND_PORT"
ASR_URL="http://127.0.0.1:$ASR_PORT/health"
LOG_DIR="$RUNTIME_DIR/logs"
CRED_DIR="$DATA_DIR/config/initial-login-credentials"

health_ok() { curl -sf -o /dev/null --max-time 2 "$1" 2>/dev/null; }

wait_url() { # $1=url $2=attempts
  local i
  for ((i = 0; i < $2; i++)); do
    health_ok "$1" && return 0
    sleep 0.7
  done
  return 1
}

proc_name() { # $1=pid → 输出进程名（去路径），不存在则输出空
  ps -p "$1" -o comm= 2>/dev/null | sed 's#.*/##' || true
}

name_matches() { # $1=实际进程名 $2=期望关键词（大小写不敏感的包含匹配）
  [ -n "$1" ] && printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | grep -q "$2"
}

service_running() { # $1=pidfile $2=期望进程名关键词 $3=health_url
  [ -f "$1" ] || return 1
  local pid name
  pid="$(tr -dc '0-9' < "$1" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  name="$(proc_name "$pid")"
  name_matches "$name" "$2" || return 1
  health_ok "$3"
}

write_pid_meta() { # $1=pid $2=pidfile $3=metafile $4=health_url
  printf '%s' "$1" > "$2"
  printf '{"pid": %s, "health_url": "%s"}\n' "$1" "$4" > "$3"
}

STARTED_PIDS=()
rollback_started() {
  warn "正在停止本次启动的进程..."
  local pid
  for pid in "${STARTED_PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" >/dev/null 2>&1 || true
  done
}

echo ""
info "正在启动本地服务。"
info "首次启动需要编译前端，通常约 1-2 分钟，请保持本窗口开启。"

command -v node >/dev/null 2>&1 || fail "未找到 Node.js，请先运行 scripts/setup-local.sh"
[ -x "$VENV_PYTHON" ] || fail "未找到后端虚拟环境（server/.venv），请先运行 scripts/setup-local.sh"
[ -f "$FRONTEND_CLI" ] || fail "未找到前端依赖（node_modules/vinext），请先运行 scripts/setup-local.sh"

mkdir -p "$RUNTIME_DIR" "$LOG_DIR" "$DATA_DIR"

BACKEND_PID_FILE="$RUNTIME_DIR/backend.pid"
FRONTEND_PID_FILE="$RUNTIME_DIR/frontend.pid"
ASR_PID_FILE="$RUNTIME_DIR/asr.pid"

# 已在运行则直接复用
if health_ok "$BACKEND_URL" && health_ok "$FRONTEND_URL"; then
  if service_running "$BACKEND_PID_FILE" python "$BACKEND_URL" && \
     service_running "$FRONTEND_PID_FILE" node "$FRONTEND_URL"; then
    ok "意向宝已经在运行：http://localhost:$FRONTEND_PORT"
  else
    warn "检测到可用的本地服务（非当前脚本托管），未改动已有进程。"
  fi
  [ "$NO_BROWSER" = 0 ] && open "$FRONTEND_URL" >/dev/null 2>&1 || true
  exit 0
fi

# ---- 1) ASR 语音服务（可选组件，缺失不阻断） ----
ASR_MODEL="$PROJECT_ROOT/models/qwen3-asr/Qwen3-ASR-1.7B.Q8_0.gguf"
ASR_MMPROJ="$PROJECT_ROOT/models/qwen3-asr/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf"
LLAMA_SERVER="${LLAMA_SERVER:-}"
if [ -z "$LLAMA_SERVER" ]; then
  if command -v llama-server >/dev/null 2>&1; then
    LLAMA_SERVER="$(command -v llama-server)"
  elif [ -x "$PROJECT_ROOT/models/llama.cpp/llama-server" ]; then
    LLAMA_SERVER="$PROJECT_ROOT/models/llama.cpp/llama-server"
  fi
fi
ASR_AVAILABLE=0
if [ -n "$LLAMA_SERVER" ] && [ -f "$ASR_MODEL" ] && [ -f "$ASR_MMPROJ" ]; then
  ASR_AVAILABLE=1
fi

if health_ok "$ASR_URL"; then
  ok "ASR 语音服务已在运行（端口 $ASR_PORT）。"
elif [ "$ASR_AVAILABLE" = 1 ]; then
  info "正在启动 ASR 语音服务（端口 $ASR_PORT）..."
  (
    cd "$(dirname "$LLAMA_SERVER")"
    nohup "$LLAMA_SERVER" -m "$ASR_MODEL" --mmproj "$ASR_MMPROJ" \
      --host 127.0.0.1 --port "$ASR_PORT" \
      > "$LOG_DIR/asr.out.log" 2> "$LOG_DIR/asr.err.log" &
    echo $! > "$ASR_PID_FILE"
  )
  ASR_PID="$(tr -dc '0-9' < "$ASR_PID_FILE")"
  STARTED_PIDS+=("$ASR_PID")
  if wait_url "$ASR_URL" 120; then
    write_pid_meta "$ASR_PID" "$ASR_PID_FILE" "$RUNTIME_DIR/asr.meta.json" "$ASR_URL"
    ok "ASR 语音服务已连接。"
  else
    warn "ASR 服务启动超时；录音转写与说话人分离将不可用。日志：$LOG_DIR/asr.err.log"
  fi
else
  warn "未找到 llama-server 或 ASR 模型（models/qwen3-asr），语音转写与说话人分离不可用。安装：brew install llama.cpp"
fi

# ---- 2) 后端服务 ----
BACKEND_PID=""
if ! health_ok "$BACKEND_URL"; then
  info "正在启动后端服务（端口 $BACKEND_PORT）..."
  # 用 Python 自己生成入口文件，避免 shell 转义路径出问题（与 Windows 版行为一致）
  "$VENV_PYTHON" - "$DATA_DIR" "$BACKEND_ROOT" "$RUNTIME_DIR/intent_bao_backend_entry.py" <<'PY'
import pathlib
import sys

data_dir = pathlib.Path(sys.argv[1]).resolve()
backend_root = pathlib.Path(sys.argv[2]).resolve()
entry = pathlib.Path(sys.argv[3])
entry.write_text(
    "import sys\n"
    f"sys.path.insert(0, {str(backend_root)!r})\n"
    "from app.main import create_app\n"
    f"app = create_app(data_dir={str(data_dir)!r})\n",
    encoding="utf-8",
)
PY
  (
    cd "$RUNTIME_DIR"
    nohup "$VENV_PYTHON" -m uvicorn intent_bao_backend_entry:app \
      --host 127.0.0.1 --port "$BACKEND_PORT" \
      > "$LOG_DIR/backend.out.log" 2> "$LOG_DIR/backend.err.log" &
    echo $! > "$BACKEND_PID_FILE"
  )
  BACKEND_PID="$(tr -dc '0-9' < "$BACKEND_PID_FILE")"
  STARTED_PIDS+=("$BACKEND_PID")
fi

# ---- 3) 前端服务 ----
FRONTEND_PID=""
if ! health_ok "$FRONTEND_URL"; then
  info "正在启动前端服务（端口 $FRONTEND_PORT）..."
  (
    cd "$PROJECT_ROOT"
    NEXT_PUBLIC_INTENT_BAO_API_BASE="http://127.0.0.1:$BACKEND_PORT/api" \
      nohup node "$FRONTEND_CLI" dev --hostname 127.0.0.1 --port "$FRONTEND_PORT" \
      > "$LOG_DIR/frontend.out.log" 2> "$LOG_DIR/frontend.err.log" &
    echo $! > "$FRONTEND_PID_FILE"
  )
  FRONTEND_PID="$(tr -dc '0-9' < "$FRONTEND_PID_FILE")"
  STARTED_PIDS+=("$FRONTEND_PID")
fi

# ---- 等待就绪 ----
if ! wait_url "$BACKEND_URL" 90; then
  [ -n "$BACKEND_PID" ] && tail -20 "$LOG_DIR/backend.err.log" 2>/dev/null || true
  rollback_started
  fail "后端未就绪（$BACKEND_URL）。请确认端口 $BACKEND_PORT 未被占用，并查看 $LOG_DIR/backend.err.log"
fi
if ! wait_url "$FRONTEND_URL" 180; then
  [ -n "$FRONTEND_PID" ] && tail -20 "$LOG_DIR/frontend.err.log" 2>/dev/null || true
  rollback_started
  fail "前端未就绪（$FRONTEND_URL）。请确认端口 $FRONTEND_PORT 未被占用，并查看 $LOG_DIR/frontend.err.log"
fi

write_pid_meta "$BACKEND_PID" "$BACKEND_PID_FILE" "$RUNTIME_DIR/backend.meta.json" "$BACKEND_URL"
write_pid_meta "$FRONTEND_PID" "$FRONTEND_PID_FILE" "$RUNTIME_DIR/frontend.meta.json" "$FRONTEND_URL"

if [ "$NO_BROWSER" = 0 ]; then
  open "$FRONTEND_URL" >/dev/null 2>&1 || true
fi

echo ""
ok "意向宝已启动："
info "  应用：http://localhost:$FRONTEND_PORT"
info "  后端：http://127.0.0.1:$BACKEND_PORT/docs"
if health_ok "$ASR_URL"; then
  info "  ASR语音服务：已连接"
else
  info "  ASR语音服务：未启动/不可用"
fi

# 首次登录凭据提示（不在屏幕上打印密码，与 Windows 版一致）
if compgen -G "$CRED_DIR/*.txt" >/dev/null 2>&1; then
  warn "首次登录凭据已在本机生成，请保存到密码管理器后删除对应文件："
  ls "$CRED_DIR"/*.txt | sed 's/^/    /'
fi
info "默认使用模拟分析；如需调用本地模型，请在页面“模型设置”中测试并保存。"
