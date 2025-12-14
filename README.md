# 币安交易所 Telegram 止损管理机器人

这是一个基于 Python 的交易机器人，用于管理币安合约交易的止损订单。机器人通过 Telegram Bot 提供用户交互界面，支持基于 K 线收盘价确认的止损策略。

## ✨ 主要功能

1. **实时监控**
   - 通过 WebSocket 实时获取币安合约的仓位变化
   - 实时监控委托订单状态
   - 即时推送更新到 Telegram

2. **智能止损**
   - 支持设置基于 K 线收盘确认的止损订单
   - 与币安原生委托分离，避免假突破
   - 支持多种时间周期：15分钟、1小时、4小时
   - K 线收盘后价格确认再执行止损

3. **便捷管理**
   - Telegram Bot 图形化交互界面
   - 按钮式操作，简单易用
   - 支持添加、删除止损订单
   - 查看当前持仓和止损状态

4. **自动维护**
   - 自动清理已平仓交易对的止损订单
   - 持续监控仓位变化
   - 异常情况自动通知

## 📋 系统要求

- Python 3.8+
- 币安合约账户 API Key
- Telegram Bot Token

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

复制配置文件模板：

```bash
cp config.ini.example config.ini
```

编辑 `config.ini`，填写以下信息：

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

#### 获取币安 API Key

1. 登录币安账户
2. 进入 API 管理页面
3. 创建新的 API Key
4. 设置权限：读取、合约交易
5. 保存 API Key 和 Secret（妥善保管）

#### 创建 Telegram Bot

1. 在 Telegram 中搜索 @BotFather
2. 发送 `/newbot` 创建新机器人
3. 按提示设置名称
4. 获得 Bot Token

#### 获取 Chat ID

1. 在 Telegram 中搜索 @userinfobot
2. 点击 Start 获取你的 User ID
3. 将 User ID 填入 `chat_id`

### 3. 运行

```bash
python main.py
```

## 📱 Telegram Bot 命令

### 基础命令

- `/start` - 启动机器人
- `/help` - 查看帮助信息
- `/positions` - 查看当前持仓
- `/orders` - 查看币安委托订单
- `/stoplosses` - 查看所有止损订单

### 止损管理

- `/addstoploss` - 添加止损订单
  1. 选择要止损的持仓
  2. 选择 K 线周期（15m/1h/4h）
  3. 输入止损价格
  
- `/deletestoploss` - 删除止损订单
  1. 选择要删除的订单
  2. 确认删除

- `/cancel` - 取消当前操作

## 🔧 工作原理

### 止损触发机制

1. **价格监控**：系统持续获取指定时间周期的 K 线数据
2. **K 线确认**：等待 K 线完全收盘
3. **价格判断**：使用收盘价判断是否触发止损条件
   - 多头止损：收盘价 ≤ 止损价
   - 空头止损：收盘价 ≥ 止损价
4. **执行止损**：条件满足后立即下市价单平仓
5. **通知推送**：通过 Telegram 实时通知执行结果

### 与币安委托的区别

| 特性 | Bot 止损 | 币安委托 |
|------|---------|---------|
| 触发时机 | K 线收盘后 | 实时价格触发 |
| 假突破 | 可避免 | 容易触发 |
| 灵活性 | 高 | 低 |
| 网络依赖 | 需要保持运行 | 无依赖 |

**建议**：将 Bot 止损与币安止损结合使用，Bot 止损用于正常止损，币安止损设置在更远位置作为安全网。

## 📂 项目结构

```
binance-telegram/
├── main.py                 # 主程序入口
├── binance_client.py       # 币安 API 客户端
├── database.py             # 数据库管理
├── stop_loss_manager.py    # 止损管理器
├── telegram_bot.py         # Telegram Bot
├── config.ini              # 配置文件（需自行创建）
├── config.ini.example      # 配置文件模板
├── requirements.txt        # Python 依赖
├── README.md              # 说明文档
├── .gitignore             # Git 忽略文件
├── trading_data.db        # SQLite 数据库（自动创建）
└── trading_bot.log        # 运行日志（自动创建）
```

## ⚠️ 注意事项

1. **安全性**
   - 妥善保管 API Key 和 Secret
   - 不要将 `config.ini` 提交到版本控制系统
   - API Key 建议只开启必要权限

2. **网络要求**
   - 需要稳定的网络连接
   - 建议在 VPS 或云服务器上运行
   - 确保能访问币安和 Telegram 服务

3. **风险提示**
   - 本机器人仅供学习和参考
   - 使用前请在测试网充分测试
   - 交易有风险，投资需谨慎
   - 作者不对使用本程序造成的损失负责

4. **运维建议**
   - 使用 screen 或 tmux 保持程序在后台运行
   - 定期检查日志文件
   - 备份数据库文件
   - 监控程序运行状态

## 🔍 故障排除

### 连接问题

**问题**：无法连接币安 API

**解决方案**：
- 检查 API Key 和 Secret 是否正确
- 确认 API 权限设置
- 检查网络连接
- 如果在中国大陆，可能需要使用代理

### Telegram Bot 无响应

**问题**：Telegram Bot 不响应命令

**解决方案**：
- 检查 Bot Token 是否正确
- 确认 Chat ID 是否正确
- 查看日志文件中的错误信息
- 尝试重新启动程序

### 止损未触发

**问题**：价格达到止损位但未执行

**解决方案**：
- 检查是否等待 K 线收盘
- 确认使用的是收盘价而非实时价
- 查看日志确认监控是否正常运行
- 检查持仓是否还存在

## 📝 开发计划

- [ ] 支持更多时间周期
- [ ] 添加追踪止损功能
- [ ] 支持条件单
- [ ] 性能优化
- [ ] 添加回测功能
- [ ] Web 管理界面

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

MIT License

## 📧 联系方式

如有问题或建议，请通过 GitHub Issues 联系。

---

**免责声明**：本软件按"原样"提供，不提供任何明示或暗示的保证。使用本软件进行交易的风险由用户自行承担。

