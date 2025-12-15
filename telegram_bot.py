"""
Telegram Bot 模块
提供用户交互界面，设置和管理止损订单
"""
import asyncio
import logging
from typing import Dict, List, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
 SELECTING_DELETE_ORDER) = range(4)


class TelegramBot:
    """Telegram Bot 管理类"""
    
    def __init__(self, token: str, chat_id: str, database: Database, 
                 stop_loss_manager: StopLossManager):
        self.token = token
        self.chat_id = chat_id
        self.database = database
        self.stop_loss_manager = stop_loss_manager
        self.application = None
        
        # 临时存储用户输入
        self.user_data_cache = {}

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
            entry_points=[CommandHandler("addstoploss", self.cmd_add_stop_loss)],
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
            entry_points=[CommandHandler("deletestoploss", self.cmd_delete_stop_loss)],
            states={
                SELECTING_DELETE_ORDER: [CallbackQueryHandler(self.select_delete_order)]
            },
            fallbacks=[CommandHandler("cancel", self.cmd_cancel)],
            per_message=False,
            per_chat=True,
            per_user=True
        )
        self.application.add_handler(delete_stop_loss_conv)
        
        # 回调查询处理器
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        
        # 初始化并启动
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        
        logger.info("Telegram Bot 已启动")

    async def stop(self):
        """停止 Telegram Bot"""
        if self.application:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
        logger.info("Telegram Bot 已停止")

    async def send_message(self, text: str, retry_count: int = 3):
        """发送消息到指定的 chat，带重试机制"""
        for attempt in range(retry_count):
            try:
                await self.application.bot.send_message(chat_id=self.chat_id, text=text)
                return  # 发送成功，退出
            except Exception as e:
                logger.error(f"发送消息失败 (尝试 {attempt + 1}/{retry_count}): {e}")
                if attempt < retry_count - 1:
                    # 等待一段时间后重试（指数退避）
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error(f"发送消息最终失败，已重试 {retry_count} 次")

    # ==================== 命令处理器 ====================
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /start 命令"""
        welcome_text = (
            "🤖 欢迎使用币安止损管理 Bot！\n\n"
            "这个 Bot 可以帮助您管理基于 K 线确认的止损订单。\n\n"
            "使用 /help 查看所有可用命令。"
        )
        await update.message.reply_text(welcome_text)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /help 命令"""
        help_text = (
            "📚 可用命令列表：\n\n"
            "/start - 开始使用\n"
            "/help - 显示帮助信息\n"
            "/positions - 查看当前持仓\n"
            "/orders - 查看币安委托订单\n"
            "/stoplosses - 查看所有止损订单\n"
            "/addstoploss - 添加止损订单\n"
            "/deletestoploss - 删除止损订单\n"
            "/cancel - 取消当前操作\n\n"
            "⚠️ 注意：\n"
            "• Bot 的止损订单独立于币安委托\n"
            "• 止损会在 K 线收盘后价格确认时触发\n"
            "• 支持的时间周期：15m, 1h, 4h"
        )
        await update.message.reply_text(help_text)

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /positions 命令 - 查看当前持仓"""
        try:
            positions = await self.stop_loss_manager.binance_client.get_positions()
            
            if not positions:
                await update.message.reply_text("📭 当前没有持仓")
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
            
            await update.message.reply_text(text)
            
        except Exception as e:
            await update.message.reply_text(f"❌ 获取持仓失败: {e}")

    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /orders 命令 - 查看币安委托订单"""
        try:
            orders = await self.stop_loss_manager.binance_client.get_open_orders()
            
            if not orders:
                await update.message.reply_text("📭 当前没有币安委托订单")
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
            
            await update.message.reply_text(text)
            
        except Exception as e:
            await update.message.reply_text(f"❌ 获取订单失败: {e}")

    async def cmd_stop_losses(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /stoplosses 命令 - 查看所有止损订单"""
        stop_losses = self.database.get_all_stop_losses()
        
        if not stop_losses:
            await update.message.reply_text("📭 当前没有止损订单")
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
        
        await update.message.reply_text(text)

    async def cmd_add_stop_loss(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /addstoploss 命令 - 开始添加止损订单流程"""
        try:
            logger.info(f"用户 {update.message.from_user.id} 执行 /addstoploss 命令")
            # 获取当前持仓
            positions = await self.stop_loss_manager.binance_client.get_positions()
            logger.info(f"获取到 {len(positions)} 个持仓")
            
            if not positions:
                await update.message.reply_text("📭 当前没有持仓，无法添加止损订单")
                return ConversationHandler.END
            
            # 创建按钮
            keyboard = []
            for pos in positions:
                button_text = f"{pos['symbol']} ({pos['side']})"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"symbol_{pos['symbol']}_{pos['side']}")])
            
            keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "请选择要设置止损的持仓：",
                reply_markup=reply_markup
            )
            
            logger.info(f"已发送持仓选择消息给用户 {update.message.from_user.id}")
            return SELECTING_SYMBOL
            
        except Exception as e:
            logger.error(f"执行 /addstoploss 命令时出错: {e}", exc_info=True)
            await update.message.reply_text(f"❌ 获取持仓失败: {e}")
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
            
            # 解析选择的交易对和方向
            parts = query.data.split("_")
            if len(parts) < 3:
                logger.error(f"回调数据格式错误: {query.data}")
                await query.edit_message_text("❌ 数据格式错误，请重新开始")
                return ConversationHandler.END
                
            symbol = parts[1]
            side = parts[2]
            logger.info(f"选择交易对: {symbol}, 方向: {side}")
            
            # 保存到用户数据
            user_id = query.from_user.id
            self.user_data_cache[user_id] = {'symbol': symbol, 'side': side}
            
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
        """处理 /deletestoploss 命令 - 删除止损订单"""
        stop_losses = self.database.get_all_stop_losses()
        
        if not stop_losses:
            await update.message.reply_text("📭 当前没有止损订单")
            return ConversationHandler.END
        
        # 创建按钮
        keyboard = []
        for order in stop_losses:
            button_text = f"ID:{order.id} {order.symbol} {order.side} @ {order.stop_price}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"delete_{order.id}")])
        
        keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "请选择要删除的止损订单：",
            reply_markup=reply_markup
        )
        
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

    # ==================== 通知方法 ====================
    
    async def notify_position_update(self, position: Dict):
        """通知持仓更新（开仓或持仓变化）"""
        text = (
            f"📊 持仓更新\n\n"
            f"交易对: {position['symbol']}\n"
            f"方向: {position['side']}\n"
            f"数量: {position['position_amt']}\n"
            f"开仓价: {position['entry_price']}\n"
            f"未实现盈亏: {position['unrealized_pnl']:.2f} USDT"
        )
        await self.send_message(text)

    async def notify_position_closed(self, data: Dict):
        """通知平仓"""
        text = (
            f"🔒 持仓已平仓\n\n"
            f"交易对: {data['symbol']}\n"
            f"方向: {data['previous_side']}\n"
            f"数量: {data['previous_amount']}"
        )
        await self.send_message(text)

    async def notify_order_update(self, order: Dict):
        """通知订单更新"""
        text = (
            f"📋 订单更新\n\n"
            f"交易对: {order['symbol']}\n"
            f"订单ID: {order['order_id']}\n"
            f"方向: {order['side']}\n"
            f"类型: {order['type']}\n"
            f"状态: {order['status']}\n"
            f"价格: {order['price']}\n"
            f"数量: {order['quantity']}"
        )
        await self.send_message(text)

    async def notify_stop_loss_triggered(self, data: Dict):
        """通知止损触发"""
        action = data['action']
        
        if action == 'executed':
            order = data['order']
            text = (
                f"🛡️ 止损已执行！\n\n"
                f"交易对: {order['symbol']}\n"
                f"方向: {order['side']}\n"
                f"触发价: {data['trigger_price']}\n"
                f"止损价: {order['stop_price']}\n"
                f"周期: {order['timeframe']}\n\n"
                f"市价单已提交"
            )
        elif action == 'failed':
            order = data['order']
            text = (
                f"❌ 止损执行失败！\n\n"
                f"交易对: {order['symbol']}\n"
                f"错误: {data['error']}"
            )
        elif action == 'cleaned':
            deleted_count = data.get('deleted_count', 0)
            text = (
                f"🧹 自动清理\n\n"
                f"交易对: {data['symbol']}\n"
                f"原因: {data['reason']}\n"
                f"已删除止损订单: {deleted_count} 个"
            )
        else:
            text = f"未知操作: {action}"
        
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

