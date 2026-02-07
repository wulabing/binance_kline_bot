# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

币安合约止损管理机器人 - 基于 K 线收盘价确认的止损系统，通过 Telegram Bot 进行交互。
纯 Python asyncio 异步架构，无测试框架、无 linter 配置。

## 常用命令

```bash
# 启动/停止/重启（后台运行，使用虚拟环境 .binance-telegram-venv）
bash start.sh          # nohup 后台启动，输出到 nohup.out
bash stop.sh
bash restart.sh

# 前台调试运行
source .binance-telegram-venv/bin/activate && python main.py

# 查看日志
tail -f trading_bot.log    # 主日志（logging 模块输出）
tail -f nohup.out          # 后台运行 stdout/stderr
```

## 架构概览

```
main.py (TradingBot) ─── 组件编排 + 回调注册
    ├── binance_client.py (BinanceClient)      # 币安 REST API + WebSocket
    ├── telegram_bot.py (TelegramBot)          # Telegram 交互 (python-telegram-bot)
    ├── stop_loss_manager.py (StopLossManager)  # K线止损监控引擎
    └── database.py (Database + StopLossOrder)  # SQLite 持久化
```

### 回调驱动的组件通信

`TradingBot.setup_callbacks()` 是理解组件协作的关键入口。各组件通过回调函数松耦合连接：

```
BinanceClient ──on_position_update──→ TradingBot ──→ TelegramBot.notify_position_update()
              ──on_position_closed──→ TradingBot ──→ TelegramBot.notify_position_closed()
              ──on_order_update────→ TradingBot ──→ TelegramBot.notify_order_update()

StopLossManager ──on_stop_loss_triggered──→ TradingBot ──→ TelegramBot.notify_stop_loss_triggered()
                ──on_evaluation_notification──→ TradingBot ──→ TelegramBot.notify_evaluation()
```

### 异步运行时

- 整个应用运行在单个 `asyncio` 事件循环中
- `TradingBot.start()` 使用 `asyncio.run()` 启动
- WebSocket 连接、K线监控、健康检查均为独立的 `asyncio.Task`
- 并发保护：`BinanceClient` 使用 `asyncio.Lock` 保护订单缓存

### 双向持仓 Key 约定

全局统一使用 `{symbol}_{side}` 作为持仓标识（如 `BTCUSDT_LONG`、`ETHUSDT_SHORT`）。
此约定贯穿 `BinanceClient` 持仓缓存、`StopLossManager` 止损匹配、`Database` 查询。

### WebSocket 连接与重连

- **用户数据流**: 持仓/订单/账户更新，通过 `listenKey` 鉴权，30分钟自动续期
- **K线数据流**: `StopLossManager` 按需通过 REST API 获取（非持久 WebSocket）
- **重连策略**: 指数退避 5s→60s，重连后调用 `_check_missed_orders()` 补漏
- **心跳**: ping 间隔 20s，超时 10s

### Telegram Bot 会话状态机

`TelegramBot` 使用 `ConversationHandler` 管理多步骤对话，状态常量定义在类顶部：

```
添加止损: SELECTING_SYMBOL(0) → SELECTING_TIMEFRAME(1) → ENTERING_PRICE(2)
删除止损: SELECTING_DELETE_ORDER(3)
更新止损: SELECTING_UPDATE_ORDER(4) → SELECTING_UPDATE_FIELD(5) → UPDATING_PRICE(6)/UPDATING_TIMEFRAME(7)
```

用户中间数据存储在 `context.user_data` 字典中（symbol, side, timeframe 等）。

### 止损监控流程

`StopLossManager._monitor_stop_losses()` 每 5 秒轮询一次：
1. 从 Database 获取所有活跃止损订单
2. 按 `(symbol, timeframe)` 分组，创建并发监控任务
3. REST API 获取最近 2 根 K 线（倒数第二根为已收盘）
4. 仅评估已收盘且未处理过的 K 线（通过 `last_checked_kline` 去重）
5. 多头: 收盘价 ≤ 止损价 → 触发；空头: 收盘价 ≥ 止损价 → 触发
6. 触发后执行市价单平仓，通过回调通知 Telegram

### 数据模型

`StopLossOrder` (database.py) — SQLite 表 `stop_loss_orders`:

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| symbol | TEXT | 交易对 (如 BTCUSDT) |
| side | TEXT | 持仓方向 (LONG/SHORT) |
| stop_price | REAL | 止损触发价 |
| timeframe | TEXT | K线周期 (15m/1h/4h) |
| quantity | REAL | 平仓数量 (NULL=全部平仓) |
| created_at | TEXT | 创建时间 |
| updated_at | TEXT | 更新时间 |

## 配置文件

`config.ini`（从 `config.ini.example` 复制）:
- `[binance]`: api_key, api_secret, testnet (true/false)
- `[telegram]`: bot_token, chat_id
- `[trading]`: default_timeframe (15m/1h/4h), enable_evaluation_notification (true/false，控制K线评估通知)
- `[database]`: db_path

## 关键依赖

- `aiohttp` — 币安 REST API 请求
- `websockets` — 币安 WebSocket 连接
- `python-telegram-bot` (v20.x) — Telegram Bot 异步 API
- Python 3.8+，无额外构建工具
