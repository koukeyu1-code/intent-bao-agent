#!/bin/bash
# 双击启动意向宝（macOS）。会打开终端窗口运行 scripts/start-local.sh
cd "$(dirname "$0")" || exit 1
exec scripts/start-local.sh
