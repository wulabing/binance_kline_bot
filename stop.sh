#!/bin/bash

# 币安 Telegram 止损机器人停止脚本

echo "================================"
echo "币安 Telegram 止损机器人 - 停止"
echo "================================"
echo ""

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# 检查PID文件
if [ ! -f ".pid" ]; then
    echo "错误: 未找到 .pid 文件"
    echo "提示: 请使用 ./start.sh 启动机器人"
    exit 1
fi

# 读取PID
pid=$(cat .pid)

# 检查进程是否存在
if ! ps -p $pid > /dev/null 2>&1; then
    echo "警告: 进程 $pid 不存在（可能已经停止）"
    rm -f .pid
    echo "已清理 .pid 文件"
    exit 0
fi

# 停止进程
echo "找到进程: $pid"
echo "正在停止机器人（等待数据库操作完成）..."
kill $pid

# 等待进程正常结束
wait_time=0
while ps -p $pid > /dev/null 2>&1; do
    sleep 1
    wait_time=$((wait_time + 1))
    dots=$((wait_time % 4))
    progress=""
    for i in $(seq 1 $dots); do
        progress="${progress}."
    done
    printf "\r等待进程正常退出 [%3ds]%s   " $wait_time "$progress"
done

printf "\n"
rm -f .pid
echo ""
echo "✓ 机器人已停止（等待时间: ${wait_time}秒）"
