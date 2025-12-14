#!/bin/bash

# 币安 Telegram 止损机器人启动脚本

echo "================================"
echo "币安 Telegram 止损机器人"
echo "================================"
echo ""

# 检查 Python 版本
python_version=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
required_version="3.8"

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
if ! pip3 list | grep -q "python-telegram-bot"; then
    echo "警告: 依赖包未安装或不完整"
    echo "正在安装依赖..."
    pip3 install -r requirements.txt
fi

echo "✓ 依赖检查完成"
echo ""

# 启动程序
echo "启动机器人..."
echo "按 Ctrl+C 停止"
echo ""

python3 main.py

