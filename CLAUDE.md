# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Binance Futures 止损交易机器人，通过 Telegram Bot 提供用户交互界面，基于K线收盘价执行止损逻辑。支持币安双向持仓（Hedge Mode）。

Python 3.10 + asyncio，无框架，纯脚本架构。

## Commands

```bash
# 启动（后台 nohup，写 .pid 文件）
./start.sh

# 停止（读 .pid，发 SIGTERM，等待优雅退出）
./stop.sh

# 重启
./restart.sh

# 虚拟环境
source .binance-telegram-venv/bin/activate
pip install -r requirements.txt

# 直接前台运行（调试用）
python main.py
```

没有测试框架、没有 linter、没有 CI。日志输出到 `trading_bot.log` 和 stdout。

## Architecture

```
main.py              — 入口，TradingBot 编排类，组装组件并设置回调
binance_client.py    — 币安 REST API + WebSocket User Data Stream
stop_loss_manager.py — K线收盘止损引擎，监控+执行
telegram_bot.py      — Telegram Bot 交互层（命令、会话、通知）
database.py          — SQLite 存储层（止损订单 CRUD）
```

### Data Flow

1. `BinanceClient` 通过 WebSocket 接收持仓/订单变更事件
2. `TradingBot` 通过回调函数桥接事件到 `TelegramBot` 发送通知
3. `StopLossManager` 轮询K线数据，收盘价触发止损时调用 `BinanceClient.place_market_order`
4. 用户通过 Telegram 命令（ConversationHandler 多步会话）管理止损订单
5. `Database` (SQLite WAL) 持久化止损订单

### Key Patterns

- **回调驱动**: `BinanceClient` 和 `StopLossManager` 通过 `on_xxx` 回调属性通知上层，`TradingBot.setup_callbacks()` 统一注册
- **双向持仓**: 所有持仓用 `{symbol}_{side}` (如 `BTCUSDT_LONG`) 作为唯一 key
- **K线收盘止损**: 不用实时价格，等K线完全收盘后评估，`last_kline_close_time` 防重复处理
- **评估批量通知**: 同一周期的多个币种评估结果延迟 8 秒合并发送
- **WebSocket 重连对账**: 重连后通过 REST API 全量比对持仓和订单缓存
- **后台任务生命周期**: `_track_task()` + `_background_tasks` set，优雅停机时统一 cancel

### Telegram Bot Commands

`/start` `/help` `/positions` `/orders` `/stoplosses` `/addstoploss` `/updatestoploss` `/deletestoploss` `/cancel`

添加/更新/删除止损使用 `ConversationHandler` 多步会话流程（InlineKeyboard 选择 + 文本输入）。

## Config

`config.ini`（从 `config.ini.example` 复制），包含 `[binance]` `[telegram]` `[trading]` `[database]` 四个 section。

## Key Conventions

- 所有异步，基于 `asyncio.run()`
- 日志统一用 `logging.getLogger(__name__)`
- 数据库每次操作独立 `connect/close`，WAL 模式
- HTTP 请求带指数退避重试（`_request` 方法）
- 进程管理通过 `.pid` 文件 + shell 脚本
