#!/bin/bash
# 双击停止意向宝（macOS）
cd "$(dirname "$0")" || exit 1
exec scripts/stop-local.sh
