"""
Telegram Bot 模块
提供用户交互界面，设置和管理止损订单
"""
import asyncio
import functools
import logging
import time
from typing import Dict, List, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters
)
from database import Database
from stop_loss_manager import StopLossManager

logger = logging.getLogger(__name__)

# 会话状态
(SELECTING_SYMBOL, SELECTING_TIMEFRAME, ENTERING_PRICE, 
 SELECTING_DELETE_ORDER, SELECTING_UPDATE_ORDER, SELECTING_UPDATE_FIELD,
 UPDATING_PRICE, UPDATING_TIMEFRAME) = range(8)


class TelegramBot:
    """Telegram Bot 管理类"""

    NOTIFICATION_SEPARATOR_LENGTH = 14
    NOTIFICATION_TOP_SEPARATOR = '═' * NOTIFICATION_SEPARATOR_LENGTH
    NOTIFICATION_BOTTOM_SEPARATOR = '─' * NOTIFICATION_SEPARATOR_LENGTH

    def _build_notification_header(self, title: str) -> str:
        """构建统一通知标题头"""
        return (
            f"{self.NOTIFICATION_TOP_SEPARATOR}\n"
            f"{title}\n"
            f"{self.NOTIFICATION_TOP_SEPARATOR}\n\n"
        )
    
    def __init__(self, token: str, chat_id: str, database: Database, 
                 stop_loss_manager: StopLossManager):
        self.token = token
        self.chat_id = chat_id
        self.database = database
        self.stop_loss_manager = stop_loss_manager
        self.application = None
        
        # 授权的 chat_id 列表（支持多个）
        self.allowed_chat_ids = {str(chat_id)}

        # 临时存储用户输入（带 TTL 自动清理）
        # 格式: {user_id: {'_created_at': timestamp, ...其他数据}}
        self.user_data_cache = {}
        self.user_data_cache_ttl = 600  # 10分钟过期
        self.cache_cleanup_task = None

        # 消息发送失败计数器和健康检查
        self.failed_send_count = 0
        self.last_successful_send = time.time()
        self.health_check_interval = 300  # 5分钟检查一次
        self.health_check_task = None

    def _is_authorized(self, update: Update) -> bool:
        """检查用户是否有权限操作 Bot"""
        chat_id = str(update.effective_chat.id) if update.effective_chat else None
        return chat_id in self.allowed_chat_ids

    async def _unauthorized_handler(self, update: Update):
        """处理未授权的访问"""
        user = update.effective_user
        chat_id = update.effective_chat.id if update.effective_chat else 'unknown'
        logger.warning(f"未授权访问: user_id={user.id if user else 'unknown'}, chat_id={chat_id}")

    async def _cache_cleanup_loop(self):
        """定期清理过期的 user_data_cache 条目"""
        while True:
            try:
                await asyncio.sleep(60)  # 每60秒检查一次
                now = time.time()
                expired_keys = [
                    uid for uid, data in self.user_data_cache.items()
                    if now - data.get('_created_at', 0) > self.user_data_cache_ttl
                ]
                for uid in expired_keys:
                    del self.user_data_cache[uid]
                if expired_keys:
                    logger.info(f"清理了 {len(expired_keys)} 个过期的会话缓存")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"缓存清理任务出错: {e}")

    async def start(self):
        """启动 Telegram Bot"""
        # 配置连接参数，增强网络容错性
        from telegram.ext import Defaults
        from telegram.request import HTTPXRequest
        
        # 创建自定义请求对象，设置更长的超时和重试
        request = HTTPXRequest(
            connection_pool_size=8,
            connect_timeout=30.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=30.0
        )
        
        self.application = (
            Application.builder()
            .token(self.token)
            .request(request)
            .build()
        )
        
        # 添加命令处理器
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("help", self.cmd_help))
        self.application.add_handler(CommandHandler("positions", self.cmd_positions))
        self.application.add_handler(CommandHandler("orders", self.cmd_orders))
        self.application.add_handler(CommandHandler("stoplosses", self.cmd_stop_losses))
        
        # 添加止损订单会话处理器
        add_stop_loss_conv = ConversationHandler(
            entry_points=[
                CommandHandler("addstoploss", self.cmd_add_stop_loss),
                CallbackQueryHandler(self.cmd_add_stop_loss, pattern="^help_addstoploss$"),
            ],
            states={
                SELECTING_SYMBOL: [CallbackQueryHandler(self.select_symbol)],
                SELECTING_TIMEFRAME: [CallbackQueryHandler(self.select_timeframe)],
                ENTERING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.enter_price)]
            },
            fallbacks=[CommandHandler("cancel", self.cmd_cancel)],
            per_message=False,
            per_chat=True,
            per_user=True
        )
        self.application.add_handler(add_stop_loss_conv)
        
        # 删除止损订单会话处理器
        delete_stop_loss_conv = ConversationHandler(
            entry_points=[
                CommandHandler("deletestoploss", self.cmd_delete_stop_loss),
                CallbackQueryHandler(self.cmd_delete_stop_loss, pattern="^help_deletestoploss$"),
            ],
            states={
                SELECTING_DELETE_ORDER: [CallbackQueryHandler(self.select_delete_order)]
            },
            fallbacks=[CommandHandler("cancel", self.cmd_cancel)],
            per_message=False,
            per_chat=True,
            per_user=True
        )
        self.application.add_handler(delete_stop_loss_conv)
        
        # 更新止损订单会话处理器
        update_stop_loss_conv = ConversationHandler(
            entry_points=[
                CommandHandler("updatestoploss", self.cmd_update_stop_loss),
                CallbackQueryHandler(self.cmd_update_stop_loss, pattern="^help_updatestoploss$"),
            ],
            states={
                SELECTING_UPDATE_ORDER: [CallbackQueryHandler(self.select_update_order)],
                SELECTING_UPDATE_FIELD: [CallbackQueryHandler(self.select_update_field)],
                UPDATING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.update_price)],
                UPDATING_TIMEFRAME: [CallbackQueryHandler(self.update_timeframe)]
            },
            fallbacks=[CommandHandler("cancel", self.cmd_cancel)],
            per_message=False,
            per_chat=True,
            per_user=True
        )
        self.application.add_handler(update_stop_loss_conv)
        
        # 回调查询处理器
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        
        # 初始化并启动
        await self.application.initialize()
        await self.application.start()
        
        # 设置 Bot 命令菜单
        await self.set_bot_commands()
        
        await self.application.updater.start_polling()
        
        # 启动健康检查任务
        self.health_check_task = asyncio.create_task(self._health_check_loop())

        # 启动缓存清理任务
        self.cache_cleanup_task = asyncio.create_task(self._cache_cleanup_loop())

        logger.info("Telegram Bot 已启动（含健康检查和缓存清理）")

    async def set_bot_commands(self):
        """设置 Bot 命令菜单"""
        commands = [
            BotCommand("start", "开始使用"),
            BotCommand("help", "显示帮助信息"),
            BotCommand("positions", "查看当前持仓"),
            BotCommand("orders", "查看币安委托订单"),
            BotCommand("stoplosses", "查看所有止损订单"),
            BotCommand("addstoploss", "添加止损订单"),
            BotCommand("updatestoploss", "更新止损订单"),
            BotCommand("deletestoploss", "删除止损订单"),
            BotCommand("cancel", "取消当前操作"),
        ]
        
        try:
            await self.application.bot.set_my_commands(commands)
            logger.info("Bot 命令菜单已设置")
        except Exception as e:
            logger.error(f"设置 Bot 命令菜单失败: {e}")

    async def _reinitialize_connection(self):
        """重新初始化 Telegram Bot 连接

        当发送消息多次失败时调用此方法重新建立连接。
        使用公共 API 而非操作私有属性，确保版本兼容性。
        """
        try:
            logger.info("正在重新初始化 Telegram Bot 连接...")

            if self.application:
                # 使用公共 API 重启：先停止再重新初始化
                try:
                    await self.application.bot.close()
                except Exception as e:
                    logger.warning(f"关闭旧 Bot 连接时出错: {e}")

                try:
                    await self.application.bot.initialize()
                except Exception as e:
                    logger.warning(f"重新初始化 Bot 时出错: {e}")

                logger.info("Telegram Bot 连接重新初始化成功")
                
        except Exception as e:
            logger.error(f"重新初始化 Telegram Bot 连接失败: {e}", exc_info=True)
            raise

    async def _health_check_loop(self):
        """定期健康检查任务
        
        每隔一段时间检查连接健康状态，如果发现异常则主动重新初始化
        """
        while True:
            try:
                await asyncio.sleep(self.health_check_interval)
                
                # 检查上次成功发送消息的时间
                time_since_last_success = time.time() - self.last_successful_send
                
                # 如果连续失败次数过多，或者很久没有成功发送过消息
                if self.failed_send_count >= 5:
                    logger.warning(
                        f"检测到连续 {self.failed_send_count} 次发送失败，"
                        f"执行主动健康检查..."
                    )
                    try:
                        # 尝试发送测试消息
                        test_message = "🔍 系统健康检查"
                        await self.application.bot.send_message(
                            chat_id=self.chat_id,
                            text=test_message,
                            read_timeout=10,
                            write_timeout=10,
                            connect_timeout=10
                        )
                        logger.info("健康检查通过，连接正常")
                        self.failed_send_count = 0
                        self.last_successful_send = time.time()
                    except Exception as e:
                        logger.error(f"健康检查失败: {e}")
                        # 尝试重新初始化连接
                        await self._reinitialize_connection()
                        
            except asyncio.CancelledError:
                logger.info("健康检查任务已取消")
                break
            except Exception as e:
                logger.error(f"健康检查任务错误: {e}", exc_info=True)

    async def stop(self):
        """停止 Telegram Bot"""
        # 取消健康检查任务
        if self.health_check_task:
            self.health_check_task.cancel()
            try:
                await self.health_check_task
            except asyncio.CancelledError:
                pass

        # 取消缓存清理任务
        if self.cache_cleanup_task:
            self.cache_cleanup_task.cancel()
            try:
                await self.cache_cleanup_task
            except asyncio.CancelledError:
                pass

        if self.application:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
        logger.info("Telegram Bot 已停止")

    async def send_message(self, text: str, retry_count: int = 10):
        """发送消息到指定的 chat，带增强重试机制和自动恢复

        Args:
            text: 要发送的消息文本
            retry_count: 重试次数（默认10次）
        """
        # 自动分页：Telegram 消息限制 4096 字符
        max_len = 4000
        if len(text) > max_len:
            chunks = self._split_message(text, max_len)
            for i, chunk in enumerate(chunks):
                await self._send_single_message(chunk, retry_count)
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.3)
            return

        await self._send_single_message(text, retry_count)

    @staticmethod
    def _split_message(text: str, max_len: int) -> list:
        """按换行符智能拆分长消息"""
        chunks = []
        while len(text) > max_len:
            split_pos = text.rfind('\n', 0, max_len)
            if split_pos == -1:
                split_pos = max_len
            chunks.append(text[:split_pos])
            text = text[split_pos:].lstrip('\n')
        if text:
            chunks.append(text)
        return chunks

    async def _send_single_message(self, text: str, retry_count: int = 10):
        """发送单条消息（带重试）"""
        for attempt in range(retry_count):
            try:
                # 检查 application 是否存在
                if self.application is None:
                    logger.error("Telegram application 未初始化")
                    return
                
                await self.application.bot.send_message(
                    chat_id=self.chat_id, 
                    text=text,
                    read_timeout=30,  # 增加读超时
                    write_timeout=30,  # 增加写超时
                    connect_timeout=30  # 增加连接超时
                )
                
                # 发送成功，更新计数器和时间戳
                self.failed_send_count = 0
                self.last_successful_send = time.time()
                logger.debug(f"消息发送成功: {text[:50]}...")
                return
                
            except Exception as e:
                self.failed_send_count += 1
                error_type = type(e).__name__
                logger.error(f"发送消息失败 (尝试 {attempt + 1}/{retry_count}): {error_type} - {e}")
                
                if attempt < retry_count - 1:
                    # 指数退避，但最多等待30秒
                    wait_time = min(2 ** attempt, 30)
                    logger.info(f"等待 {wait_time} 秒后重试...")
                    await asyncio.sleep(wait_time)
                    
                    # 如果连续失败3次，尝试重新初始化连接
                    if (attempt + 1) % 3 == 0:
                        logger.warning(f"连续失败 {attempt + 1} 次，尝试重新初始化 Telegram 连接...")
                        try:
                            await self._reinitialize_connection()
                        except Exception as reinit_error:
                            logger.error(f"重新初始化连接失败: {reinit_error}")
                else:
                    logger.error(
                        f"发送消息最终失败，已重试 {retry_count} 次\n"
                        f"消息内容: {text[:100]}...\n"
                        f"连续失败次数: {self.failed_send_count}"
                    )

    # ==================== 命令处理器 ====================
    
    async def _reply(self, update: Update, text: str, reply_markup=None):
        """统一回复方法：支持命令消息和按钮回调两种来源"""
        if update.message:
            await update.message.reply_text(text, reply_markup=reply_markup)
        elif update.callback_query:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup)
        else:
            await self.send_message(text)

    def _build_help_keyboard(self) -> InlineKeyboardMarkup:
        """构建帮助菜单的 InlineKeyboard 按钮"""
        keyboard = [
            [
                InlineKeyboardButton("📊 查看持仓", callback_data="help_positions"),
                InlineKeyboardButton("📋 委托订单", callback_data="help_orders"),
            ],
            [
                InlineKeyboardButton("🛡 止损订单", callback_data="help_stoplosses"),
            ],
            [
                InlineKeyboardButton("➕ 添加止损", callback_data="help_addstoploss"),
                InlineKeyboardButton("✏️ 更新止损", callback_data="help_updatestoploss"),
            ],
            [
                InlineKeyboardButton("🗑 删除止损", callback_data="help_deletestoploss"),
            ],
        ]
        return InlineKeyboardMarkup(keyboard)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /start 命令"""
        if not self._is_authorized(update):
            await self._unauthorized_handler(update)
            return
        welcome_text = (
            "🤖 欢迎使用币安止损管理 Bot！\n\n"
            "这个 Bot 可以帮助您管理基于 K 线确认的止损订单。\n\n"
            "请选择您需要的功能："
        )
        await update.message.reply_text(welcome_text, reply_markup=self._build_help_keyboard())

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /help 命令"""
        if not self._is_authorized(update):
            await self._unauthorized_handler(update)
            return
        help_text = (
            "📚 功能菜单\n\n"
            "请点击下方按钮选择功能：\n\n"
            "⚠️ 注意：\n"
            "• Bot 的止损订单独立于币安委托\n"
            "• 止损会在 K 线收盘后价格确认时触发\n"
            "• 支持的时间周期：15m, 1h, 4h"
        )
        await update.message.reply_text(help_text, reply_markup=self._build_help_keyboard())

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /positions 命令 - 查看当前持仓"""
        if not self._is_authorized(update):
            await self._unauthorized_handler(update)
            return
        try:
            positions = await self.stop_loss_manager.binance_client.get_positions()
            
            if not positions:
                await self._reply(update, "📭 当前没有持仓")
                return
            
            text = "📊 当前持仓：\n\n"
            for pos in positions:
                text += (
                    f"🔸 {pos['symbol']}\n"
                    f"  方向: {pos['side']}\n"
                    f"  数量: {pos['position_amt']}\n"
                    f"  开仓价: {pos['entry_price']}\n"
                    f"  未实现盈亏: {pos['unrealized_pnl']:.2f} USDT\n"
                    f"  杠杆: {pos['leverage']}x\n"
                    f"  强平价: {pos['liquidation_price']}\n\n"
                )
            
            await self._reply(update, text)

        except Exception as e:
            await self._reply(update, f"❌ 获取持仓失败: {e}")

    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /orders 命令 - 查看币安委托订单"""
        if not self._is_authorized(update):
            await self._unauthorized_handler(update)
            return
        try:
            orders = await self.stop_loss_manager.binance_client.get_open_orders()
            
            if not orders:
                await self._reply(update, "📭 当前没有币安委托订单")
                return
            
            text = "📋 币安委托订单：\n\n"
            for order in orders:
                text += (
                    f"🔸 {order['symbol']}\n"
                    f"  订单ID: {order['order_id']}\n"
                    f"  方向: {order['side']}\n"
                    f"  类型: {order['type']}\n"
                    f"  价格: {order['price']}\n"
                )
                
                # 添加触发价格（如果有）
                if order['stop_price'] > 0:
                    text += f"  触发价格: {order['stop_price']}\n"
                
                text += (
                    f"  数量: {order['quantity']}\n"
                    f"  状态: {order['status']}\n"
                )
                
                # 添加只减仓标识
                if order['reduce_only']:
                    text += "  只减仓: 是\n"
                else:
                    text += "  只减仓: 否\n"
                
                text += "\n"
            
            await self._reply(update, text)

        except Exception as e:
            await self._reply(update, f"❌ 获取订单失败: {e}")

    async def cmd_stop_losses(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /stoplosses 命令 - 查看所有止损订单"""
        if not self._is_authorized(update):
            await self._unauthorized_handler(update)
            return
        stop_losses = self.database.get_all_stop_losses()
        
        if not stop_losses:
            await self._reply(update, "📭 当前没有止损订单")
            return

        text = "🛡️ Bot 止损订单：\n\n"
        for order in stop_losses:
            text += (
                f"🔸 ID: {order.id}\n"
                f"  交易对: {order.symbol}\n"
                f"  方向: {order.side}\n"
                f"  止损价: {order.stop_price}\n"
                f"  周期: {order.timeframe}\n"
                f"  数量: {order.quantity if order.quantity else '全部'}\n"
                f"  创建时间: {order.created_at}\n\n"
            )
        
        await self._reply(update, text)

    async def cmd_add_stop_loss(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /addstoploss 命令或菜单按钮 - 开始添加止损订单流程"""
        # 兼容按钮回调来源
        if update.callback_query:
            await update.callback_query.answer()
        if not self._is_authorized(update):
            await self._unauthorized_handler(update)
            return ConversationHandler.END
        try:
            user = update.effective_user
            logger.info(f"用户 {user.id} 执行添加止损操作")
            # 获取当前持仓
            positions = await self.stop_loss_manager.binance_client.get_positions()
            logger.info(f"获取到 {len(positions)} 个持仓")

            if not positions:
                await self._reply(update, "📭 当前没有持仓，无法添加止损订单")
                return ConversationHandler.END

            # 创建按钮
            keyboard = []
            for pos in positions:
                button_text = f"{pos['symbol']} ({pos['side']})"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"symbol|{pos['symbol']}|{pos['side']}")])

            keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            await self._reply(update, "请选择要设置止损的持仓：", reply_markup=reply_markup)

            logger.info(f"已发送持仓选择消息给用户 {user.id}")
            return SELECTING_SYMBOL
            
        except Exception as e:
            logger.error(f"执行添加止损操作时出错: {e}", exc_info=True)
            await self._reply(update, f"❌ 获取持仓失败: {e}")
            return ConversationHandler.END

    async def select_symbol(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """选择交易对"""
        try:
            query = update.callback_query
            await query.answer()
            
            logger.info(f"用户选择回调: {query.data}")
            
            if query.data == "cancel":
                await query.edit_message_text("❌ 操作已取消")
                return ConversationHandler.END
            
            # 解析选择的交易对和方向（使用 | 分隔，避免 symbol 含下划线时解析错误）
            parts = query.data.split("|")
            if len(parts) < 3:
                logger.error(f"回调数据格式错误: {query.data}")
                await query.edit_message_text("❌ 数据格式错误，请重新开始")
                return ConversationHandler.END

            symbol = parts[1]
            side = parts[2]
            logger.info(f"选择交易对: {symbol}, 方向: {side}")
            
            # 保存到用户数据
            user_id = query.from_user.id
            self.user_data_cache[user_id] = {'symbol': symbol, 'side': side, '_created_at': time.time()}
            
            # 显示时间周期选择
            keyboard = [
                [InlineKeyboardButton("15 分钟", callback_data="timeframe_15m")],
                [InlineKeyboardButton("1 小时", callback_data="timeframe_1h")],
                [InlineKeyboardButton("4 小时", callback_data="timeframe_4h")],
                [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"已选择: {symbol} ({side})\n\n请选择 K 线周期：",
                reply_markup=reply_markup
            )
            
            logger.info(f"已发送时间周期选择消息给用户 {user_id}")
            return SELECTING_TIMEFRAME
            
        except Exception as e:
            logger.error(f"选择交易对时出错: {e}", exc_info=True)
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(f"❌ 处理失败: {e}")
            return ConversationHandler.END

    async def select_timeframe(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """选择时间周期"""
        try:
            query = update.callback_query
            await query.answer()
            
            logger.info(f"用户选择时间周期回调: {query.data}")
            
            if query.data == "cancel":
                await query.edit_message_text("❌ 操作已取消")
                return ConversationHandler.END
            
            # 解析时间周期
            parts = query.data.split("_")
            if len(parts) < 2:
                logger.error(f"时间周期回调数据格式错误: {query.data}")
                await query.edit_message_text("❌ 数据格式错误，请重新开始")
                return ConversationHandler.END
                
            timeframe = parts[1]
            
            # 保存到用户数据
            user_id = query.from_user.id
            if user_id not in self.user_data_cache:
                logger.error(f"用户 {user_id} 的会话数据不存在")
                await query.edit_message_text("❌ 会话已过期，请重新开始")
                return ConversationHandler.END
                
            self.user_data_cache[user_id]['timeframe'] = timeframe
            
            user_data = self.user_data_cache[user_id]
            
            await query.edit_message_text(
                f"已选择:\n"
                f"  交易对: {user_data['symbol']}\n"
                f"  方向: {user_data['side']}\n"
                f"  周期: {timeframe}\n\n"
                f"请输入止损价格："
            )
            
            logger.info(f"已发送价格输入提示给用户 {user_id}")
            return ENTERING_PRICE
            
        except Exception as e:
            logger.error(f"选择时间周期时出错: {e}", exc_info=True)
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(f"❌ 处理失败: {e}")
            return ConversationHandler.END

    async def enter_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """输入止损价格"""
        try:
            user_id = update.message.from_user.id
            logger.info(f"用户 {user_id} 输入价格: {update.message.text}")
            
            if user_id not in self.user_data_cache:
                logger.warning(f"用户 {user_id} 的会话数据不存在")
                await update.message.reply_text("❌ 会话已过期，请重新开始")
                return ConversationHandler.END
            
            # 解析价格
            try:
                stop_price = float(update.message.text)
            except ValueError:
                logger.warning(f"用户 {user_id} 输入的价格格式错误: {update.message.text}")
                await update.message.reply_text("❌ 价格格式错误，请输入有效数字")
                return ENTERING_PRICE
            
            user_data = self.user_data_cache[user_id]
            symbol = user_data['symbol']
            side = user_data['side']
            timeframe = user_data['timeframe']

            # 止损价格方向合理性校验
            try:
                klines = await self.stop_loss_manager.binance_client.get_kline_data(symbol, '1m', limit=1)
                if klines:
                    current_price = klines[0]['close']
                    if side == 'LONG' and stop_price >= current_price:
                        await update.message.reply_text(
                            f"⚠️ 多头止损价应低于当前价格\n"
                            f"当前价: {current_price}\n"
                            f"您输入: {stop_price}\n\n"
                            f"请重新输入止损价格："
                        )
                        return ENTERING_PRICE
                    elif side == 'SHORT' and stop_price <= current_price:
                        await update.message.reply_text(
                            f"⚠️ 空头止损价应高于当前价格\n"
                            f"当前价: {current_price}\n"
                            f"您输入: {stop_price}\n\n"
                            f"请重新输入止损价格："
                        )
                        return ENTERING_PRICE
            except Exception as e:
                logger.warning(f"获取当前价格校验失败（不阻塞创建）: {e}")

            logger.info(f"准备创建止损订单: {symbol} {side} @ {stop_price} [{timeframe}]")

            # 添加止损订单
            order_id = await self.stop_loss_manager.add_stop_loss_order(
                symbol=symbol,
                side=side,
                stop_price=stop_price,
                timeframe=timeframe
            )
            
            logger.info(f"止损订单创建成功: ID {order_id}")
            
            await update.message.reply_text(
                f"✅ 止损订单已创建！\n\n"
                f"订单ID: {order_id}\n"
                f"交易对: {symbol}\n"
                f"方向: {side}\n"
                f"止损价: {stop_price}\n"
                f"周期: {timeframe}\n\n"
                f"系统将在 {timeframe} K 线收盘后确认价格并触发止损。"
            )
            
            # 清理缓存
            del self.user_data_cache[user_id]
            
            return ConversationHandler.END
            
        except Exception as e:
            logger.error(f"创建止损订单时出错: {e}", exc_info=True)
            user_id = update.message.from_user.id
            await update.message.reply_text(f"❌ 创建止损订单失败: {e}")
            if user_id in self.user_data_cache:
                del self.user_data_cache[user_id]
            return ConversationHandler.END

    async def cmd_delete_stop_loss(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /deletestoploss 命令或菜单按钮 - 删除止损订单"""
        if update.callback_query:
            await update.callback_query.answer()
        if not self._is_authorized(update):
            await self._unauthorized_handler(update)
            return ConversationHandler.END
        stop_losses = self.database.get_all_stop_losses()

        if not stop_losses:
            await self._reply(update, "📭 当前没有止损订单")
            return ConversationHandler.END

        # 创建按钮
        keyboard = []
        for order in stop_losses:
            button_text = f"ID:{order.id} {order.symbol} {order.side} @ {order.stop_price}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"delete_{order.id}")])

        keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await self._reply(update, "请选择要删除的止损订单：", reply_markup=reply_markup)

        return SELECTING_DELETE_ORDER

    async def select_delete_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """选择要删除的订单"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "cancel":
            await query.edit_message_text("❌ 操作已取消")
            return ConversationHandler.END
        
        # 解析订单ID
        order_id = int(query.data.split("_")[1])
        
        # 删除订单
        success = self.database.delete_stop_loss(order_id)
        
        if success:
            await query.edit_message_text(f"✅ 止损订单 {order_id} 已删除")
        else:
            await query.edit_message_text(f"❌ 删除失败，订单 {order_id} 不存在")
        
        return ConversationHandler.END

    async def cmd_update_stop_loss(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /updatestoploss 命令或菜单按钮 - 更新止损价格"""
        if update.callback_query:
            await update.callback_query.answer()
        if not self._is_authorized(update):
            await self._unauthorized_handler(update)
            return ConversationHandler.END
        stop_losses = self.database.get_all_stop_losses()

        if not stop_losses:
            await self._reply(update, "📭 当前没有止损订单")
            return ConversationHandler.END

        # 创建按钮
        keyboard = []
        for order in stop_losses:
            button_text = f"ID:{order.id} {order.symbol} {order.side} @ {order.stop_price} [{order.timeframe}]"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"update_{order.id}")])

        keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await self._reply(update, "请选择要更新的止损订单：", reply_markup=reply_markup)

        return SELECTING_UPDATE_ORDER

    async def select_update_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """选择要更新的订单"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "cancel":
            await query.edit_message_text("❌ 操作已取消")
            return ConversationHandler.END
        
        # 解析订单ID
        order_id = int(query.data.split("_")[1])
        
        # 获取订单信息
        order = self.database.get_stop_loss_by_id(order_id)
        
        if not order:
            await query.edit_message_text("❌ 订单不存在")
            return ConversationHandler.END
        
        # 保存到用户数据
        user_id = query.from_user.id
        self.user_data_cache[user_id] = {'order_id': order_id, 'order': order, '_created_at': time.time()}
        
        # 显示修改选项
        keyboard = [
            [InlineKeyboardButton("💰 只修改价格", callback_data="field_price")],
            [InlineKeyboardButton("⏰ 只修改周期", callback_data="field_timeframe")],
            [InlineKeyboardButton("💰⏰ 修改价格和周期", callback_data="field_both")],
            [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"当前止损订单信息：\n\n"
            f"交易对: {order.symbol}\n"
            f"方向: {order.side}\n"
            f"当前止损价: {order.stop_price}\n"
            f"当前周期: {order.timeframe}\n\n"
            f"请选择要修改的内容：",
            reply_markup=reply_markup
        )
        
        return SELECTING_UPDATE_FIELD

    async def select_update_field(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """选择要修改的字段"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "cancel":
            await query.edit_message_text("❌ 操作已取消")
            user_id = query.from_user.id
            if user_id in self.user_data_cache:
                del self.user_data_cache[user_id]
            return ConversationHandler.END
        
        user_id = query.from_user.id
        if user_id not in self.user_data_cache:
            await query.edit_message_text("❌ 会话已过期，请重新开始")
            return ConversationHandler.END
        
        field = query.data.split("_")[1]
        self.user_data_cache[user_id]['update_field'] = field
        order = self.user_data_cache[user_id]['order']
        
        if field == "price":
            # 只修改价格
            await query.edit_message_text(
                f"当前止损价: {order.stop_price}\n\n"
                f"请输入新的止损价格："
            )
            return UPDATING_PRICE
            
        elif field == "timeframe":
            # 只修改周期
            keyboard = [
                [InlineKeyboardButton("15 分钟", callback_data="newtf_15m")],
                [InlineKeyboardButton("1 小时", callback_data="newtf_1h")],
                [InlineKeyboardButton("4 小时", callback_data="newtf_4h")],
                [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"当前周期: {order.timeframe}\n\n"
                f"请选择新的 K 线周期：",
                reply_markup=reply_markup
            )
            return UPDATING_TIMEFRAME
            
        elif field == "both":
            # 修改价格和周期，先选周期
            self.user_data_cache[user_id]['update_both'] = True
            
            keyboard = [
                [InlineKeyboardButton("15 分钟", callback_data="newtf_15m")],
                [InlineKeyboardButton("1 小时", callback_data="newtf_1h")],
                [InlineKeyboardButton("4 小时", callback_data="newtf_4h")],
                [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"当前周期: {order.timeframe}\n\n"
                f"请选择新的 K 线周期：",
                reply_markup=reply_markup
            )
            return UPDATING_TIMEFRAME

    async def update_timeframe(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """更新周期"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "cancel":
            await query.edit_message_text("❌ 操作已取消")
            user_id = query.from_user.id
            if user_id in self.user_data_cache:
                del self.user_data_cache[user_id]
            return ConversationHandler.END
        
        user_id = query.from_user.id
        if user_id not in self.user_data_cache:
            await query.edit_message_text("❌ 会话已过期，请重新开始")
            return ConversationHandler.END
        
        # 解析新周期
        new_timeframe = query.data.split("_")[1]
        
        user_data = self.user_data_cache[user_id]
        order_id = user_data['order_id']
        order = user_data['order']
        update_both = user_data.get('update_both', False)
        
        if update_both:
            # 需要继续输入价格
            self.user_data_cache[user_id]['new_timeframe'] = new_timeframe
            
            await query.edit_message_text(
                f"已选择新周期: {new_timeframe}\n"
                f"当前止损价: {order.stop_price}\n\n"
                f"请输入新的止损价格："
            )
            return UPDATING_PRICE
        else:
            # 只修改周期，直接更新
            try:
                success = self.database.update_stop_loss(order_id, timeframe=new_timeframe)
                
                if success:
                    logger.info(f"止损订单周期更新成功: ID {order_id}, {order.timeframe} -> {new_timeframe}")
                    
                    await query.edit_message_text(
                        f"✅ 止损周期已更新！\n\n"
                        f"订单ID: {order_id}\n"
                        f"交易对: {order.symbol}\n"
                        f"方向: {order.side}\n"
                        f"止损价: {order.stop_price}\n"
                        f"原周期: {order.timeframe}\n"
                        f"新周期: {new_timeframe}\n\n"
                        f"⚠️ 系统会自动停止旧周期的监控任务，并在5秒内启动新周期的监控。"
                    )
                else:
                    await query.edit_message_text(f"❌ 更新失败，订单 {order_id} 可能已不存在")
                
                # 清理缓存
                del self.user_data_cache[user_id]
                
                return ConversationHandler.END
                
            except Exception as e:
                logger.error(f"更新止损周期时出错: {e}", exc_info=True)
                await query.edit_message_text(f"❌ 更新止损周期失败: {e}")
                if user_id in self.user_data_cache:
                    del self.user_data_cache[user_id]
                return ConversationHandler.END

    async def update_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """更新止损价格"""
        try:
            user_id = update.message.from_user.id
            logger.info(f"用户 {user_id} 输入新价格: {update.message.text}")
            
            if user_id not in self.user_data_cache:
                logger.warning(f"用户 {user_id} 的会话数据不存在")
                await update.message.reply_text("❌ 会话已过期，请重新开始")
                return ConversationHandler.END
            
            # 解析新价格
            try:
                new_stop_price = float(update.message.text)
            except ValueError:
                logger.warning(f"用户 {user_id} 输入的价格格式错误: {update.message.text}")
                await update.message.reply_text("❌ 价格格式错误，请输入有效数字")
                return UPDATING_PRICE
            
            user_data = self.user_data_cache[user_id]
            order_id = user_data['order_id']
            order = user_data['order']
            new_timeframe = user_data.get('new_timeframe')
            update_both = user_data.get('update_both', False)
            
            # 根据是否同时更新周期来更新
            if update_both and new_timeframe:
                # 同时更新价格和周期
                logger.info(f"准备更新止损订单 {order_id}: 价格 {order.stop_price} -> {new_stop_price}, 周期 {order.timeframe} -> {new_timeframe}")
                
                success = self.database.update_stop_loss(
                    order_id, 
                    stop_price=new_stop_price,
                    timeframe=new_timeframe
                )
                
                if success:
                    logger.info(f"止损订单更新成功: ID {order_id}")
                    
                    await update.message.reply_text(
                        f"✅ 止损订单已更新！\n\n"
                        f"订单ID: {order_id}\n"
                        f"交易对: {order.symbol}\n"
                        f"方向: {order.side}\n"
                        f"原止损价: {order.stop_price} → 新止损价: {new_stop_price}\n"
                        f"原周期: {order.timeframe} → 新周期: {new_timeframe}\n\n"
                        f"⚠️ 系统会自动停止旧周期的监控任务，并在5秒内启动新周期的监控。"
                    )
                else:
                    await update.message.reply_text(f"❌ 更新失败，订单 {order_id} 可能已不存在")
            else:
                # 只更新价格
                logger.info(f"准备更新止损订单 {order_id}: {order.stop_price} -> {new_stop_price}")
                
                success = self.database.update_stop_loss(order_id, stop_price=new_stop_price)
                
                if success:
                    logger.info(f"止损订单价格更新成功: ID {order_id}")
                    
                    await update.message.reply_text(
                        f"✅ 止损价格已更新！\n\n"
                        f"订单ID: {order_id}\n"
                        f"交易对: {order.symbol}\n"
                        f"方向: {order.side}\n"
                        f"原止损价: {order.stop_price}\n"
                        f"新止损价: {new_stop_price}\n"
                        f"周期: {order.timeframe}"
                    )
                else:
                    await update.message.reply_text(f"❌ 更新失败，订单 {order_id} 可能已不存在")
            
            # 清理缓存
            del self.user_data_cache[user_id]
            
            return ConversationHandler.END
            
        except Exception as e:
            logger.error(f"更新止损价格时出错: {e}", exc_info=True)
            user_id = update.message.from_user.id
            await update.message.reply_text(f"❌ 更新止损价格失败: {e}")
            if user_id in self.user_data_cache:
                del self.user_data_cache[user_id]
            return ConversationHandler.END

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /cancel 命令 - 取消当前操作"""
        user_id = update.message.from_user.id
        if user_id in self.user_data_cache:
            del self.user_data_cache[user_id]
        
        await update.message.reply_text("❌ 操作已取消")
        return ConversationHandler.END

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理其他按钮回调"""
        query = update.callback_query
        await query.answer()

        if not query.data or not query.data.startswith("help_"):
            return

        command = query.data[5:]  # 去掉 help_ 前缀
        chat_id = query.message.chat_id

        # 查询类命令：直接复用命令处理函数
        handler_map = {
            "positions": self.cmd_positions,
            "orders": self.cmd_orders,
            "stoplosses": self.cmd_stop_losses,
        }

        if command in handler_map:
            await handler_map[command](update, context)
            return

    # ==================== 通知方法 ====================
    
    async def notify_position_update(self, position: Dict):
        """通知持仓更新（开仓或持仓变化）"""
        # 根据方向选择emoji
        side_icon = "🟢" if position['side'] == 'LONG' else "🔴"
        side_text = "做多" if position['side'] == 'LONG' else "做空"
        
        # 根据盈亏选择emoji和颜色
        pnl = float(position['unrealized_pnl'])
        if pnl > 0:
            pnl_icon = "💰"
            pnl_text = f"+{pnl:.2f}"
        elif pnl < 0:
            pnl_icon = "📉"
            pnl_text = f"{pnl:.2f}"
        else:
            pnl_icon = "➖"
            pnl_text = f"{pnl:.2f}"
        
        text = (
            self._build_notification_header("📊 持仓更新通知")
            + f"🏷 交易对：{position['symbol']}\n"
            f"{side_icon} 方向：{side_text} ({position['side']})\n"
            f"📦 数量：{position['position_amt']}\n"
            f"💵 开仓价：{position['entry_price']}\n"
            f"⚖️ 杠杆：{position['leverage']}x\n"
            f"{pnl_icon} 未实现盈亏：{pnl_text} USDT\n"
            f"⚠️ 强平价：{position['liquidation_price']}\n"
            + self.NOTIFICATION_BOTTOM_SEPARATOR
        )
        await self.send_message(text)

    async def notify_position_closed(self, data: Dict):
        """通知平仓"""
        # 根据方向选择emoji
        side_icon = "🟢" if data['previous_side'] == 'LONG' else "🔴"
        side_text = "做多" if data['previous_side'] == 'LONG' else "做空"
        
        text = (
            self._build_notification_header("🔒 持仓平仓通知")
            + f"🏷 交易对：{data['symbol']}\n"
            f"{side_icon} 方向：{side_text} ({data['previous_side']})\n"
            f"📦 数量：{data['previous_amount']}\n\n"
            f"✅ 该持仓已完全平仓\n"
            + self.NOTIFICATION_BOTTOM_SEPARATOR
        )
        await self.send_message(text)

    async def notify_order_update(self, order: Dict):
        """通知订单更新"""
        # 过滤订单状态，只通知重要的状态变化
        # 跳过：PARTIALLY_FILLED（部分成交）
        # 通知：NEW（新订单）、FILLED（完全成交）、CANCELED（取消）、EXPIRED（过期）、REJECTED（拒绝）
        status = order['status']
        
        if status in ['PARTIALLY_FILLED']:
            # 不发送通知，避免太多噪音
            logger.info(f"跳过订单状态通知: {order['symbol']} {status}")
            return
        
        # 根据方向选择emoji
        side_icon = "🟢" if order['side'] == 'BUY' else "🔴"
        side_text_map = {
            'BUY': '买入/做多',
            'SELL': '卖出/做空'
        }
        side_text = side_text_map.get(order['side'], order['side'])
        
        # 根据订单类型选择emoji和描述
        type_map = {
            'MARKET': ('⚡', '市价单'),
            'LIMIT': ('📌', '限价单'),
            'STOP': ('🛑', '止损单'),
            'STOP_MARKET': ('🛑', '止损市价单'),
            'TAKE_PROFIT': ('🎯', '止盈单'),
            'TAKE_PROFIT_MARKET': ('🎯', '止盈市价单'),
        }
        type_icon, type_text = type_map.get(order['type'], ('📋', order['type']))
        
        # 根据订单状态选择emoji和描述
        status_map = {
            'NEW': ('🆕', '已创建'),
            'FILLED': ('✅', '已完全成交'),
            'CANCELED': ('❌', '已取消'),
            'EXPIRED': ('⏰', '已过期'),
            'REJECTED': ('🚫', '已拒绝'),
            'PARTIALLY_FILLED': ('⏳', '部分成交'),
        }
        status_icon, status_text = status_map.get(status, ('📋', status))
        
        # 构建消息
        text = (
            self._build_notification_header("📋 订单更新通知")
            + f"🏷 交易对：{order['symbol']}\n"
            f"🆔 订单ID：{order['order_id']}\n"
            f"{side_icon} 方向：{side_text}\n"
            f"{type_icon} 类型：{type_text}\n"
            f"{status_icon} 状态：{status_text}\n"
        )
        
        # 添加价格信息
        if order.get('price') and float(order.get('price', 0)) > 0:
            text += f"💵 价格：{order['price']}\n"
        
        # 添加触发价格（如果有）
        if order.get('stop_price') and float(order['stop_price']) > 0:
            text += f"🎯 触发价：{order['stop_price']}\n"
        
        # 添加数量信息
        text += f"📦 数量：{order['quantity']}\n"
        
        # 添加已成交数量（如果有）
        executed_qty = order.get('executed_qty', 0)
        if executed_qty and float(executed_qty) > 0:
            text += f"✓ 已成交：{executed_qty}\n"
        
        # 添加只减仓标识
        if order.get('reduce_only'):
            text += f"⚠️ 只减仓：是\n"
        
        text += self.NOTIFICATION_BOTTOM_SEPARATOR
        
        await self.send_message(text)

    async def notify_stop_loss_triggered(self, data: Dict):
        """通知止损触发"""
        action = data['action']
        
        if action == 'executed':
            order = data['order']
            # 根据方向选择emoji
            side_icon = "🟢" if order['side'] == 'LONG' else "🔴"
            side_text = "做多" if order['side'] == 'LONG' else "做空"
            
            text = (
                self._build_notification_header("🛡️ 止损已触发执行！")
                + f"🏷 交易对：{order['symbol']}\n"
                f"{side_icon} 方向：{side_text} ({order['side']})\n"
                f"📊 触发价：{data['trigger_price']}\n"
                f"🎯 止损价：{order['stop_price']}\n"
                f"⏰ K线周期：{order['timeframe']}\n\n"
                f"✅ 市价单已提交，等待成交\n"
                + self.NOTIFICATION_BOTTOM_SEPARATOR
            )
        elif action == 'failed':
            order = data['order']
            text = (
                self._build_notification_header("❌ 止损执行失败！")
                + f"🏷 交易对：{order['symbol']}\n"
                f"⚠️ 错误信息：{data['error']}\n\n"
                f"🔔 请手动检查持仓状态\n"
                + self.NOTIFICATION_BOTTOM_SEPARATOR
            )
        elif action == 'cleaned':
            deleted_count = data.get('deleted_count', 0)
            side = data.get('side', '')
            side_icon = "🟢" if side == 'LONG' else "🔴"
            side_text = "做多" if side == 'LONG' else "做空"
            
            text = (
                self._build_notification_header("🧹 自动清理通知")
                + f"🏷 交易对：{data['symbol']}\n"
                f"{side_icon} 方向：{side_text} ({side})\n"
                f"📝 原因：{data['reason']}\n"
                f"🗑️ 已删除止损订单：{deleted_count} 个\n"
                + self.NOTIFICATION_BOTTOM_SEPARATOR
            )
        else:
            text = f"⚠️ 未知操作: {action}"
        
        await self.send_message(text)

    async def notify_evaluation(self, data: Dict):
        """通知K线收盘评估信息"""
        timeframe = data['timeframe']
        evaluations = data['evaluations']
        
        if not evaluations:
            return
        
        # 按交易对分组评估信息
        symbol_evaluations = {}
        for eval_data in evaluations:
            symbol = eval_data['symbol']
            if symbol not in symbol_evaluations:
                symbol_evaluations[symbol] = []
            symbol_evaluations[symbol].append(eval_data)
        
        # 构建消息文本
        text = f"📊 K线收盘评估 [{timeframe}]\n\n"
        
        for symbol, evals in symbol_evaluations.items():
            text += f"🔸 {symbol}\n"
            for eval_data in evals:
                close_price = eval_data['close_price']
                stop_price = eval_data['stop_price']
                side = eval_data['side']
                should_trigger = eval_data['should_trigger']
                
                # 计算价格差
                if side == 'LONG':
                    price_diff = close_price - stop_price
                    price_diff_pct = (price_diff / stop_price) * 100 if stop_price > 0 else 0
                else:  # SHORT
                    price_diff = stop_price - close_price
                    price_diff_pct = (price_diff / stop_price) * 100 if stop_price > 0 else 0
                
                status_icon = "🔴" if should_trigger else "🟢"
                status_text = "应执行止损" if should_trigger else "无需止损"
                
                text += (
                    f"  {status_icon} {side} | "
                    f"收盘价: {close_price:.4f} | "
                    f"止损价: {stop_price:.4f}\n"
                    f"     差价: {price_diff:+.4f} ({price_diff_pct:+.2f}%) | "
                    f"{status_text}\n"
                )
            text += "\n"
        
        await self.send_message(text)
