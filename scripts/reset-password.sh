#!/usr/bin/env bash
# 意向宝 macOS 登录密码重置脚本（对应 Windows 的 重置意向宝登录密码.bat）
# 用法：scripts/reset-password.sh [admin|planner] [--generate]
#   不带参数时进入交互选择；--generate 生成强密码，否则进入隐藏式手动输入
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/server/.venv/bin/python"

fail() { printf '\033[31m[密码重置] 错误：\033[0m %s\n' "$*" >&2; exit 1; }

[ -x "$VENV_PYTHON" ] || fail "未找到 Python 虚拟环境（server/.venv）。请先运行 scripts/setup-local.sh。"

ACCOUNT="${1:-}"
if [ -z "$ACCOUNT" ]; then
  read -r -p "重置哪个账号 [admin/planner，回车默认 admin]: " ACCOUNT
  ACCOUNT="${ACCOUNT:-admin}"
fi
case "$ACCOUNT" in
  admin|planner) ;;
  *) fail "账号只能是 admin 或 planner，收到的是：$ACCOUNT" ;;
esac

MODE="${2:-}"
if [ -z "$MODE" ]; then
  read -r -p "密码方式 [Y=生成强密码（回车默认）/ n=手动输入]: " MODE
  MODE="${MODE:-Y}"
fi

cd "$PROJECT_ROOT/server"
if [ "$MODE" = "--generate" ] || [ "$MODE" = "Y" ] || [ "$MODE" = "y" ]; then
  exec "$VENV_PYTHON" -m app.auth_admin "$ACCOUNT" --data-dir "$PWD/data" --generate
else
  echo "[提示] 输入的字符不会显示在屏幕上，这是正常现象。"
  exec "$VENV_PYTHON" -m app.auth_admin "$ACCOUNT" --data-dir "$PWD/data"
fi
