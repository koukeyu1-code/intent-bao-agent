#!/usr/bin/env bash
# 意向宝 macOS 环境安装脚本（对应 Windows 的 scripts/setup-local.ps1）
# 用法：scripts/setup-local.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_ROOT="$PROJECT_ROOT/server"
VENV_DIR="$BACKEND_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
REQUIREMENTS="$BACKEND_ROOT/requirements.txt"

info() { printf '\033[36m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[setup] 警告：\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[setup]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[setup] 错误：\033[0m %s\n' "$*" >&2; exit 1; }

# 1) Python 3.11+
PYTHON_CANDIDATES=("${PYTHON:-}" python3.13 python3.12 python3.11 python3)
PYTHON_BIN=""
for candidate in "${PYTHON_CANDIDATES[@]}"; do
  [ -n "$candidate" ] || continue
  command -v "$candidate" >/dev/null 2>&1 || continue
  if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done
[ -n "$PYTHON_BIN" ] || fail "未找到 Python 3.11+。请先安装：brew install python@3.12（或从 python.org 下载安装包），或设置 PYTHON 环境变量指向可执行文件。"
ok "Python: $("$PYTHON_BIN" --version 2>&1)（$PYTHON_BIN）"

# 2) 后端虚拟环境
if [ ! -x "$VENV_PYTHON" ]; then
  info "正在创建本地 Python 虚拟环境（server/.venv）..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
[ -x "$VENV_PYTHON" ] || fail "虚拟环境创建失败：$VENV_DIR"

# 3) 依赖（macOS 无 CUDA，直接安装 PyPI 默认 wheel，Apple Silicon 自带 MPS 加速）
info "正在安装 PyTorch（macOS 版，首次约需几分钟）..."
"$VENV_PYTHON" -m pip install --disable-pip-version-check torch torchaudio
info "正在安装后端依赖..."
"$VENV_PYTHON" -m pip install --disable-pip-version-check -r "$REQUIREMENTS" \
  || fail "后端依赖安装失败，请查看上方日志。"

# 4) Node / 前端依赖
NODE_MAJOR=0
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR="$(node -p 'parseInt(process.versions.node, 10)' 2>/dev/null || echo 0)"
fi
if [ "$NODE_MAJOR" -lt 22 ]; then
  fail "未找到 Node.js 22+（当前: $(command -v node >/dev/null 2>&1 && node --version || echo '无')）。请安装：brew install node@22 或 https://nodejs.org/"
fi
ok "Node: $(node --version)"

if [ ! -d "$PROJECT_ROOT/node_modules" ]; then
  command -v pnpm >/dev/null 2>&1 || fail "未找到 pnpm。请先安装 Node.js，然后运行 npm install -g pnpm。"
  info "正在安装前端依赖（node_modules）..."
  (cd "$PROJECT_ROOT" && pnpm install) || fail "前端依赖安装失败。"
fi

# 5) 外部工具检查（仅提示，不阻断）
if command -v ffmpeg >/dev/null 2>&1 && ffmpeg -version >/dev/null 2>&1; then
  ok "FFmpeg: 已安装"
else
  warn "未找到 FFmpeg，录音/录屏处理不可用。安装：brew install ffmpeg"
fi
if command -v ffprobe >/dev/null 2>&1; then
  ok "FFprobe: 已安装"
else
  warn "未找到 FFprobe（随 FFmpeg 一起安装），媒体时长探测不可用。"
fi

if command -v tesseract >/dev/null 2>&1; then
  if tesseract --list-langs 2>/dev/null | grep -q "chi_sim"; then
    ok "Tesseract OCR: 已安装（含中文）"
  else
    warn "Tesseract 缺少中文语言包 chi_sim，中文 OCR 不可用。安装：brew install tesseract tesseract-lang"
  fi
else
  warn "未找到 Tesseract OCR，录屏关键帧 OCR 不可用（纯录音场景不受影响）。安装：brew install tesseract tesseract-lang"
fi

if command -v ollama >/dev/null 2>&1; then
  ok "Ollama: 已安装"
else
  warn "未找到 Ollama。若使用 LM Studio 等本地 OpenAI 兼容服务可跳过；若选择 Ollama，请安装后执行 ollama pull qwen3.5:9b"
fi

LLAMA_SERVER_FOUND=0
if command -v llama-server >/dev/null 2>&1 || [ -x "$PROJECT_ROOT/models/llama.cpp/llama-server" ]; then
  LLAMA_SERVER_FOUND=1
  ok "llama-server（ASR 运行时）: 已找到"
else
  warn "未找到 llama-server，ASR 语音转写不可用。安装：brew install llama.cpp"
fi

# 6) 模型文件检查
ASR_MODEL="$PROJECT_ROOT/models/qwen3-asr/Qwen3-ASR-1.7B.Q8_0.gguf"
ASR_MMPROJ="$PROJECT_ROOT/models/qwen3-asr/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf"
SPEAKER_MODEL="$PROJECT_ROOT/models/funasr/models/iic--speech_campplus_sv_zh-cn_16k-common/snapshots/master/campplus_cn_common.bin"
VAD_MODEL="$PROJECT_ROOT/models/funasr/models/iic--speech_fsmn_vad_zh-cn-16k-common-pytorch/snapshots/master/model.pt"
MISSING=()
[ -f "$ASR_MODEL" ] || MISSING+=("models/qwen3-asr（Qwen3-ASR 主模型）")
[ -f "$ASR_MMPROJ" ] || MISSING+=("models/qwen3-asr（ASR mmproj）")
if [ "$LLAMA_SERVER_FOUND" = 0 ]; then MISSING+=("llama-server 运行时（brew install llama.cpp）"); fi
[ -f "$SPEAKER_MODEL" ] || MISSING+=("models/funasr（cam++ 声纹权重）")
[ -f "$VAD_MODEL" ] || MISSING+=("models/funasr（fsmn-vad 权重）")
if [ "${#MISSING[@]}" -gt 0 ]; then
  warn "语音链路组件缺失：${MISSING[*]}。缺失期间业务流程可用模拟分析，语音转写与说话人分离不可用。"
else
  ok "模型文件齐全（ASR 主模型 + mmproj + FunASR 声纹/VAD）"
fi

echo ""
ok "意向宝本地环境准备完成。"
info "下一步：运行 scripts/start-local.sh（或双击 启动意向宝.command）启动。"
