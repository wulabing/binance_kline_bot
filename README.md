# 币安合约止损管理机器人

基于 **K 线收盘价确认** 的币安合约止损管理机器人，通过 Telegram Bot 进行交互。

## 功能特性

- WebSocket 实时监控币安合约仓位和订单变化
- 基于 K 线收盘价确认的止损订单（15m / 1h / 4h）
- Telegram Bot 交互界面，InlineKeyboard 按钮式操作
- 自动清理已平仓交易对的止损订单
- 支持币安双向持仓模式（Hedge Mode）
- WebSocket 断线自动重连 + REST 全量对账
- 同周期多币种评估结果延迟合并通知
- 后台任务统一生命周期管理，支持优雅停机

## 环境要求

- Python 3.10+
- 币安 API Key（需开启「读取」和「合约交易」权限）
- Telegram Bot Token + Chat ID
- 稳定的网络连接（建议 VPS 部署）

## 快速开始

### 1. 克隆项目

```bash
git clone <repo-url>
cd binance-telegram
```

### 2. 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> 启动脚本会自动检测虚拟环境（按 `.venv` → `venv` → `.binance-telegram-venv` 顺序查找），未找到时自动创建 `.venv`。也可通过环境变量 `VENV_DIR` 指定自定义路径。

### 3. 配置

```bash
cp config.ini.example config.ini
```

编辑 `config.ini`：

```ini
[binance]
api_key = YOUR_BINANCE_API_KEY
api_secret = YOUR_BINANCE_API_SECRET
testnet = false

[telegram]
bot_token = YOUR_TELEGRAM_BOT_TOKEN
chat_id = YOUR_TELEGRAM_CHAT_ID

[trading]
default_timeframe = 15m

[database]
db_path = trading_data.db
```

### 4. 启动 / 停止 / 重启

```bash
# 后台启动
./start.sh

# 停止（优雅退出，等待数据库操作完成）
./stop.sh

# 重启
./restart.sh

# 指定自定义虚拟环境路径启动
VENV_DIR=/path/to/your/venv ./start.sh

# 前台运行（调试用）
source .venv/bin/activate
python main.py
```

## Telegram 命令

| 命令 | 说明 |
|------|------|
| `/start` | 启动机器人 |
| `/help` | 查看帮助 |
| `/positions` | 查看当前持仓 |
| `/orders` | 查看币安委托订单 |
| `/stoplosses` | 查看止损订单 |
| `/addstoploss` | 添加止损（多步会话） |
| `/updatestoploss` | 更新止损价格 |
| `/deletestoploss` | 删除止损 |
| `/cancel` | 取消当前操作 |

支持 InlineKeyboard 功能菜单，可直接从按钮触发止损会话流程。

## 止损触发逻辑

1. 实时获取指定周期的 K 线数据
2. 等待 K 线完全收盘
3. 使用收盘价判断：
   - 多头止损：收盘价 ≤ 止损价
   - 空头止损：收盘价 ≥ 止损价
4. 条件满足后下市价单平仓
5. Telegram 通知执行结果

## 项目结构

```
main.py              — 入口，TradingBot 编排类，组装组件并设置回调
binance_client.py    — 币安 REST API + WebSocket User Data Stream
stop_loss_manager.py — K线收盘止损引擎，监控 + 执行
telegram_bot.py      — Telegram Bot 交互层（命令、会话、通知）
database.py          — SQLite 存储层（止损订单 CRUD，WAL 模式）
config.ini.example   — 配置模板
requirements.txt     — Python 依赖
start.sh             — 启动脚本（自动检测虚拟环境）
stop.sh              — 停止脚本（优雅退出）
restart.sh           — 重启脚本
```

## 注意事项

- 妥善保管 API Key，不要提交 `config.ini` 到版本控制
- 币安 API 需要开启「读取」和「合约交易」权限
- 建议在 VPS 上运行以保证网络稳定性
- Bot 止损独立于币安委托止损，建议配合使用
- 测试网充分测试后再用于实盘

## 许可

MIT License
