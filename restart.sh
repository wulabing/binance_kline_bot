#!/bin/bash

# 币安 Telegram 止损机器人重启脚本

echo "================================"
echo "币安 Telegram 止损机器人 - 重启"
echo "================================"
echo ""

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# 切换到脚本目录
cd "$SCRIPT_DIR"

# 停止机器人
echo "步骤 1/2: 停止现有进程..."
echo ""
bash stop.sh

echo ""
echo "================================"
echo ""

# 等待一秒确保进程完全结束
sleep 1

# 启动机器人
echo "步骤 2/2: 启动机器人..."
echo ""
bash start.sh

echo ""
echo "✓ 重启完成"
