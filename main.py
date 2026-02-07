"""
主程序
整合币安客户端、止损管理器和 Telegram Bot
"""
import asyncio
import configparser
import logging
import signal
import sys
from pathlib import Path

from binance_client import BinanceClient
from database import Database
from stop_loss_manager import StopLossManager
from telegram_bot import TelegramBot

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


class TradingBot:
    """交易机器人主类"""
    
    def __init__(self, config_path: str = 'config.ini'):
        self.config_path = config_path
        self.config = None
        
        self.binance_client = None
        self.database = None
        self.stop_loss_manager = None
        self.telegram_bot = None
        
        self.running = False

    def load_config(self):
        """加载配置文件"""
        if not Path(self.config_path).exists():
            logger.error(f"配置文件不存在: {self.config_path}")
            logger.info("请复制 config.ini.example 为 config.ini 并填写配置")
            sys.exit(1)
        
        config = configparser.ConfigParser()
        config.read(self.config_path, encoding='utf-8')
        
        # 验证必要的配置项
        required_sections = ['binance', 'telegram', 'database']
        for section in required_sections:
            if section not in config:
                logger.error(f"配置文件缺少 [{section}] 部分")
                sys.exit(1)

        # [trading] 段为可选，缺失时使用默认值
        if 'trading' not in config:
            config['trading'] = {}
            logger.info("[trading] 配置段缺失，使用默认值")
        
        self.config = config
        logger.info("配置文件加载成功")

    def initialize_components(self):
        """初始化所有组件"""
        # 初始化数据库
        db_path = self.config['database']['db_path']
        self.database = Database(db_path)
        logger.info(f"数据库初始化: {db_path}")
        
        # 初始化币安客户端
        api_key = self.config['binance']['api_key']
        api_secret = self.config['binance']['api_secret']
        testnet = self.config['binance'].getboolean('testnet', False)
        
        self.binance_client = BinanceClient(api_key, api_secret, testnet)
        logger.info(f"币安客户端初始化 (测试网: {testnet})")
        
        # 读取评估通知配置
        enable_evaluation_notification = self.config['trading'].getboolean('enable_evaluation_notification', True)
        
        # 初始化止损管理器
        self.stop_loss_manager = StopLossManager(
            self.binance_client, 
            self.database,
            enable_evaluation_notification=enable_evaluation_notification
        )
        logger.info(f"止损管理器初始化 (评估通知: {'启用' if enable_evaluation_notification else '禁用'})")
        
        # 初始化 Telegram Bot
        bot_token = self.config['telegram']['bot_token']
        chat_id = self.config['telegram']['chat_id']
        
        self.telegram_bot = TelegramBot(
            bot_token, chat_id, self.database, self.stop_loss_manager
        )
        logger.info("Telegram Bot 初始化")

    def setup_callbacks(self):
        """设置回调函数"""
        # 币安客户端的回调
        self.binance_client.on_position_update = self.on_position_update
        self.binance_client.on_position_closed = self.on_position_closed
        self.binance_client.on_order_update = self.on_order_update
        self.binance_client.on_account_update = self.on_account_update
        
        # 止损管理器的回调
        self.stop_loss_manager.on_stop_loss_triggered = self.on_stop_loss_triggered
        self.stop_loss_manager.on_evaluation_notification = self.on_evaluation_notification
        
        logger.info("回调函数设置完成")

    async def on_position_update(self, position):
        """持仓更新回调（开仓或持仓变化）"""
        logger.info(f"持仓更新: {position}")
        await self.telegram_bot.notify_position_update(position)

    async def on_position_closed(self, data):
        """平仓回调"""
        logger.info(f"持仓已平仓: {data}")
        await self.telegram_bot.notify_position_closed(data)

    async def on_order_update(self, order):
        """订单更新回调"""
        logger.info(f"订单更新: {order}")
        await self.telegram_bot.notify_order_update(order)

    async def on_account_update(self, data):
        """账户更新回调"""
        logger.debug(f"账户更新: {data}")

    async def on_stop_loss_triggered(self, data):
        """止损触发回调"""
        logger.info(f"止损触发: {data}")
        await self.telegram_bot.notify_stop_loss_triggered(data)

    async def on_evaluation_notification(self, data):
        """评估信息通知回调"""
        logger.info(f"K线收盘评估: {data['timeframe']}, {len(data['evaluations'])} 个评估")
        await self.telegram_bot.notify_evaluation(data)

    async def start(self):
        """启动交易机器人"""
        try:
            logger.info("=" * 50)
            logger.info("交易机器人启动中...")
            logger.info("=" * 50)
            
            # 加载配置
            self.load_config()
            
            # 初始化组件
            self.initialize_components()
            
            # 设置回调
            self.setup_callbacks()
            
            # 启动 Telegram Bot
            await self.telegram_bot.start()
            await self.telegram_bot.send_message("🚀 交易机器人已启动！")
            
            # 启动止损管理器
            await self.stop_loss_manager.start()
            
            # 初始化持仓缓存（避免首次更新时误判为开仓）
            await self.initialize_position_cache()
            
            # 初始化订单缓存（避免启动时误判为新订单）
            await self.initialize_order_cache()
            
            # 启动币安 WebSocket 用户数据流
            asyncio.create_task(self.binance_client.start_user_data_stream())
            
            self.running = True
            logger.info("=" * 50)
            logger.info("交易机器人运行中...")
            logger.info("按 Ctrl+C 停止")
            logger.info("=" * 50)
            
            # 发送启动通知，包含当前持仓信息
            await self.send_startup_info()
            
            # 保持运行
            while self.running:
                await asyncio.sleep(1)
                
        except KeyboardInterrupt:
            logger.info("收到停止信号")
        except Exception as e:
            logger.error(f"启动失败: {e}", exc_info=True)
            raise
        finally:
            await self.stop()

    async def initialize_position_cache(self):
        """初始化持仓缓存（支持双向持仓）"""
        try:
            positions = await self.binance_client.get_positions()
            for pos in positions:
                # 根据方向设置正负值
                position_amt = pos['position_amt'] if pos['side'] == 'LONG' else -pos['position_amt']
                # 使用 symbol_side 组合作为key
                position_key = f"{pos['symbol']}_{pos['side']}"
                self.binance_client.position_cache[position_key] = position_amt
            logger.info(f"持仓缓存初始化完成，当前持仓数: {len(positions)}")
        except Exception as e:
            logger.warning(f"初始化持仓缓存失败: {e}")
    
    async def initialize_order_cache(self):
        """初始化订单缓存，避免启动时误判为新订单"""
        try:
            orders = await self.binance_client.get_open_orders()
            for order in orders:
                order_id = order['order_id']
                self.binance_client.order_cache[order_id] = order
            logger.info(f"订单缓存初始化完成，当前委托订单数: {len(orders)}")
        except Exception as e:
            logger.warning(f"初始化订单缓存失败: {e}")

    async def send_startup_info(self):
        """发送启动信息"""
        try:
            # 获取当前持仓
            positions = await self.binance_client.get_positions()
            
            # 获取止损订单
            stop_losses = self.database.get_all_stop_losses()
            
            info_text = "📊 启动信息\n\n"
            
            # 持仓信息
            if positions:
                info_text += f"持仓数量: {len(positions)}\n"
                for pos in positions:
                    info_text += f"  • {pos['symbol']} {pos['side']}\n"
            else:
                info_text += "持仓数量: 0\n"
            
            info_text += "\n"
            
            # 止损订单信息
            if stop_losses:
                info_text += f"止损订单: {len(stop_losses)}\n"
                for order in stop_losses:
                    info_text += f"  • {order.symbol} {order.side} @ {order.stop_price} [{order.timeframe}]\n"
            else:
                info_text += "止损订单: 0\n"
            
            await self.telegram_bot.send_message(info_text)
            
        except Exception as e:
            logger.error(f"发送启动信息失败: {e}")

    async def stop(self):
        """停止交易机器人"""
        logger.info("=" * 50)
        logger.info("交易机器人停止中...")
        logger.info("=" * 50)
        
        self.running = False
        
        try:
            # 停止止损管理器
            if self.stop_loss_manager:
                try:
                    await self.stop_loss_manager.stop()
                except Exception as e:
                    logger.warning(f"停止止损管理器时出错: {e}")
            
            # 关闭币安客户端
            if self.binance_client:
                try:
                    await self.binance_client.close()
                except Exception as e:
                    logger.warning(f"关闭币安客户端时出错: {e}")
            
            # 停止 Telegram Bot（先发送消息，稍等片刻确保消息发送成功）
            if self.telegram_bot:
                try:
                    await self.telegram_bot.send_message("🛑 交易机器人已停止")
                    await asyncio.sleep(0.5)  # 等待消息发送完成
                except Exception as e:
                    logger.warning(f"发送停止消息失败: {e}")
                
                try:
                    await self.telegram_bot.stop()
                except Exception as e:
                    logger.warning(f"停止 Telegram Bot 时出错: {e}")
            
            logger.info("所有组件已关闭")
            
        except Exception as e:
            logger.error(f"停止过程中出错: {e}")
        
        logger.info("=" * 50)
        logger.info("交易机器人已停止")
        logger.info("=" * 50)


async def main():
    """主函数"""
    bot = TradingBot()
    
    # 获取当前事件循环
    loop = asyncio.get_running_loop()
    
    # 设置信号处理（使用 asyncio 友好的方式）
    def signal_handler():
        if bot.running:
            bot.running = False
            print("\n收到停止信号，正在关闭程序...")
    
    # 注册信号处理器到事件循环
    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)
    
    try:
        # 启动机器人
        await bot.start()
    finally:
        # 移除信号处理器
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序异常退出: {e}", exc_info=True)
        sys.exit(1)

