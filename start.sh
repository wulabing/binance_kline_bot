#!/bin/bash

# 币安 Telegram 止损机器人启动脚本

echo "================================"
echo "币安 Telegram 止损机器人"
echo "================================"
echo ""

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# 检查是否已经在运行
if [ -f ".pid" ]; then
    old_pid=$(cat .pid)
    if ps -p $old_pid > /dev/null 2>&1; then
        echo "错误: 机器人已在运行 (PID: $old_pid)"
        echo "如需重启，请先运行: ./stop.sh"
        exit 1
    else
        echo "清理过期的PID文件..."
        rm -f .pid
    fi
fi

# 虚拟环境自动检测
# 优先级: $VENV_DIR 环境变量 > .venv > venv > .binance-telegram-venv
NEED_INSTALL=false
if [ -z "$VENV_DIR" ]; then
    for candidate in .venv venv .binance-telegram-venv; do
        if [ -d "$candidate" ]; then
            VENV_DIR="$candidate"
            break
        fi
    done
fi

if [ -z "$VENV_DIR" ]; then
    echo "未找到虚拟环境，正在创建 .venv ..."
    python3 -m venv .venv || { echo "错误: 创建虚拟环境失败"; exit 1; }
    VENV_DIR=".venv"
    NEED_INSTALL=true
fi

# 激活虚拟环境
echo "激活虚拟环境..."
source "$VENV_DIR/bin/activate"
echo "✓ 虚拟环境已激活: $VENV_DIR"
echo ""

# 检查 Python 版本
python_version=$(python --version 2>&1 | grep -oP '\d+\.\d+')
required_version="3"

if (( $(echo "$python_version < $required_version" | bc -l) )); then
    echo "错误: 需要 Python $required_version 或更高版本"
    echo "当前版本: $python_version"
    exit 1
fi

echo "✓ Python 版本检查通过: $python_version"

# 检查配置文件
if [ ! -f "config.ini" ]; then
    echo "错误: 配置文件 config.ini 不存在"
    echo "请复制 config.ini.example 为 config.ini 并填写配置"
    exit 1
fi

echo "✓ 配置文件存在"

# 检查依赖
echo ""
echo "检查 Python 依赖..."
if [ "$NEED_INSTALL" = true ] || ! pip show python-telegram-bot > /dev/null 2>&1; then
    echo "正在安装依赖..."
    pip install -r requirements.txt || { echo "错误: 依赖安装失败"; exit 1; }
fi

echo "✓ 依赖检查完成"
echo ""

# 启动程序
echo "启动机器人..."
echo "按 Ctrl+C 停止"
echo ""

nohup python main.py > nohup.out 2>&1 &
echo $! > .pid

echo "✓ 机器人已启动 (PID: $(cat .pid))"
echo "日志文件: nohup.out"


