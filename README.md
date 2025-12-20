# 币安合约止损管理机器人

基于 **K 线收盘价确认** 的币安合约止损管理机器人，通过 Telegram Bot 进行交互。

## 功能

- WebSocket 实时监控币安合约仓位和订单变化
- 基于 K 线收盘价确认的止损订单（15m/1h/4h）
- Telegram Bot 交互界面，按钮式操作
- 自动清理已平仓交易对的止损订单
- 支持双向持仓模式

## 安装

```bash
pip install -r requirements.txt
```

## 配置

复制配置模板并编辑：

```bash
cp config.ini.example config.ini
```

配置项说明：

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

## 运行

```bash
python main.py
```

## Telegram 命令

- `/start` - 启动机器人
- `/help` - 查看帮助
- `/positions` - 查看持仓
- `/orders` - 查看币安委托订单
- `/stoplosses` - 查看止损订单
- `/addstoploss` - 添加止损
- `/updatestoploss` - 更新止损价格
- `/deletestoploss` - 删除止损
- `/cancel` - 取消操作

## 止损触发逻辑

1. 实时获取指定周期的 K 线数据
2. 等待 K 线收盘
3. 使用收盘价判断：
   - 多头止损：收盘价 ≤ 止损价
   - 空头止损：收盘价 ≥ 止损价
4. 条件满足后下市价单平仓
5. Telegram 通知执行结果

## 文件说明

- `main.py` - 程序入口
- `binance_client.py` - 币安 API 封装
- `telegram_bot.py` - Telegram Bot 交互
- `stop_loss_manager.py` - 止损逻辑
- `database.py` - SQLite 数据库
- `config.ini` - 配置文件（需自行创建）
- `trading_data.db` - 数据库文件（自动创建）
- `trading_bot.log` - 运行日志（自动创建）

## 注意事项

- 妥善保管 API Key，不要提交 `config.ini` 到版本控制
- 币安 API 需要开启「读取」和「合约交易」权限
- 需要稳定的网络连接，建议在 VPS 上运行
- Bot 止损独立于币安委托，建议配合使用
- 测试网充分测试后再用于实盘

## 许可

MIT License
