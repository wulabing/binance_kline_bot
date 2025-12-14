"""
止损管理器模块
负责监控K线并执行止损逻辑
"""
import asyncio
import logging
from typing import Dict, List, Callable, Optional
from datetime import datetime
from database import Database, StopLossOrder
from binance_client import BinanceClient

logger = logging.getLogger(__name__)


class StopLossManager:
    """止损管理器"""
    
    def __init__(self, binance_client: BinanceClient, database: Database):
        self.binance_client = binance_client
        self.database = database
        
        # 存储每个交易对最新的K线收盘时间
        self.last_kline_close_time = {}
        
        # 回调函数
        self.on_stop_loss_triggered = None
        
        # 监控任务
        self.monitoring_tasks = {}
        
        # 当前持仓缓存
        self.current_positions = {}
        
        # 运行状态
        self.running = False

    async def start(self):
        """启动止损管理器"""
        logger.info("启动止损管理器")
        self.running = True
        
        # 立即初始化持仓缓存（避免监控任务启动时缓存为空）
        try:
            positions = await self.binance_client.get_positions()
            self.current_positions = {pos['symbol']: pos for pos in positions}
            logger.info(f"止损管理器持仓缓存初始化完成，当前持仓数: {len(positions)}")
        except Exception as e:
            logger.warning(f"初始化止损管理器持仓缓存失败: {e}")
            self.current_positions = {}
        
        # 启动持仓检查任务
        asyncio.create_task(self._check_positions_loop())
        
        # 启动止损监控任务
        asyncio.create_task(self._monitor_stop_losses())
    
    async def stop(self):
        """停止止损管理器"""
        logger.info("停止止损管理器")
        self.running = False

    async def _check_positions_loop(self):
        """定期检查持仓，清理已平仓交易对的止损订单"""
        while self.running:
            try:
                await asyncio.sleep(30)  # 每30秒检查一次
                
                if not self.running:
                    break
                
                # 获取当前所有持仓
                positions = await self.binance_client.get_positions()
                
                # 更新持仓缓存
                self.current_positions = {pos['symbol']: pos for pos in positions}
                
                # 获取数据库中所有的止损订单
                all_stop_losses = self.database.get_all_stop_losses()
                
                # 收集需要清理的交易对（去重，避免同一交易对的多个订单重复处理）
                symbols_to_clean = set()
                for order in all_stop_losses:
                    if order.symbol not in self.current_positions:
                        symbols_to_clean.add(order.symbol)
                
                # 对每个需要清理的交易对，删除订单并发送通知
                for symbol in symbols_to_clean:
                    # 删除该交易对的所有止损订单
                    deleted_count = self.database.delete_stop_losses_by_symbol(symbol)
                    if deleted_count > 0:
                        logger.info(f"清理已平仓交易对 {symbol} 的 {deleted_count} 个止损订单")
                        
                        if self.on_stop_loss_triggered:
                            await self.on_stop_loss_triggered({
                                'action': 'cleaned',
                                'symbol': symbol,
                                'reason': '仓位已不存在',
                                'deleted_count': deleted_count
                            })
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"检查持仓时出错: {e}")

    async def _monitor_stop_losses(self):
        """监控所有止损订单"""
        while self.running:
            try:
                await asyncio.sleep(5)  # 每5秒检查一次
                
                if not self.running:
                    break
                
                # 获取所有止损订单
                all_stop_losses = self.database.get_all_stop_losses()
                
                # 按交易对和时间周期分组
                monitoring_groups = {}
                for order in all_stop_losses:
                    key = f"{order.symbol}_{order.timeframe}"
                    if key not in monitoring_groups:
                        monitoring_groups[key] = []
                    monitoring_groups[key].append(order)
                
                # 为每个组创建监控任务
                for key, orders in monitoring_groups.items():
                    if key not in self.monitoring_tasks or self.monitoring_tasks[key].done():
                        symbol = orders[0].symbol
                        timeframe = orders[0].timeframe
                        task = asyncio.create_task(self._monitor_symbol_timeframe(symbol, timeframe))
                        self.monitoring_tasks[key] = task
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"监控止损订单时出错: {e}")

    async def _monitor_symbol_timeframe(self, symbol: str, timeframe: str):
        """监控特定交易对和时间周期的止损"""
        logger.info(f"开始监控 {symbol} [{timeframe}] 的止损")
        
        # 转换时间周期为秒数
        interval_seconds = self._timeframe_to_seconds(timeframe)
        
        while self.running:
            try:
                # 获取该交易对和时间周期的所有止损订单
                all_orders = self.database.get_all_stop_losses()
                orders = [o for o in all_orders if o.symbol == symbol and o.timeframe == timeframe]
                
                if not orders:
                    # 没有订单了，退出监控
                    logger.info(f"停止监控 {symbol} [{timeframe}]，无止损订单")
                    break
                
                # 检查是否有持仓
                if symbol not in self.current_positions:
                    logger.info(f"停止监控 {symbol} [{timeframe}]，无持仓")
                    break
                
                # 获取最新的K线数据
                klines = await self.binance_client.get_kline_data(symbol, timeframe, limit=1)
                
                if not klines:
                    await asyncio.sleep(5)
                    continue
                
                latest_kline = klines[0]
                close_time = latest_kline['close_time']
                current_time = int(datetime.now().timestamp() * 1000)
                
                # 检查K线是否已经收盘
                if current_time >= close_time:
                    # K线已收盘，使用收盘价
                    price = latest_kline['close']
                    
                    # 检查是否是新的K线（避免重复触发）
                    last_close = self.last_kline_close_time.get(f"{symbol}_{timeframe}", 0)
                    if close_time > last_close:
                        self.last_kline_close_time[f"{symbol}_{timeframe}"] = close_time
                        
                        # 检查每个止损订单
                        for order in orders:
                            await self._check_stop_loss_trigger(order, price)
                
                # 等待一段时间再检查
                await asyncio.sleep(min(10, interval_seconds // 6))
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self.running:
                    break
                logger.error(f"监控 {symbol} [{timeframe}] 时出错: {e}")
                await asyncio.sleep(5)

    async def _check_stop_loss_trigger(self, order: StopLossOrder, current_price: float):
        """检查止损是否触发"""
        triggered = False
        
        # 获取持仓信息
        position = self.current_positions.get(order.symbol)
        if not position:
            logger.warning(f"止损订单 {order.id} 对应的持仓不存在: {order.symbol}")
            return
        
        # 判断是否触发止损
        if order.side == 'LONG':
            # 多头止损：当前价格 <= 止损价格
            if current_price <= order.stop_price:
                triggered = True
        else:  # SHORT
            # 空头止损：当前价格 >= 止损价格
            if current_price >= order.stop_price:
                triggered = True
        
        if triggered:
            logger.warning(
                f"止损触发！{order.symbol} {order.side} @ {current_price} "
                f"(止损价: {order.stop_price}, 周期: {order.timeframe})"
            )
            
            # 执行止损
            await self._execute_stop_loss(order, position, current_price)

    async def _execute_stop_loss(self, order: StopLossOrder, position: Dict, trigger_price: float):
        """执行止损"""
        try:
            # 确定平仓方向（多头平仓=卖出，空头平仓=买入）
            side = 'SELL' if order.side == 'LONG' else 'BUY'
            
            # 确定平仓数量
            quantity = order.quantity if order.quantity else position['position_amt']
            
            logger.info(
                f"执行止损: {order.symbol} {side} {quantity} "
                f"(触发价: {trigger_price}, 止损价: {order.stop_price})"
            )
            
            # 下市价单
            result = await self.binance_client.place_market_order(
                symbol=order.symbol,
                side=side,
                quantity=quantity
            )
            
            logger.info(f"止损订单已执行: {result}")
            
            # 删除已执行的止损订单
            self.database.delete_stop_loss(order.id)
            
            # 触发回调
            if self.on_stop_loss_triggered:
                await self.on_stop_loss_triggered({
                    'action': 'executed',
                    'order': order.to_dict(),
                    'trigger_price': trigger_price,
                    'result': result
                })
            
        except Exception as e:
            logger.error(f"执行止损失败: {e}")
            
            if self.on_stop_loss_triggered:
                await self.on_stop_loss_triggered({
                    'action': 'failed',
                    'order': order.to_dict(),
                    'error': str(e)
                })

    def _timeframe_to_seconds(self, timeframe: str) -> int:
        """将时间周期转换为秒数"""
        mapping = {
            '1m': 60,
            '3m': 180,
            '5m': 300,
            '15m': 900,
            '30m': 1800,
            '1h': 3600,
            '2h': 7200,
            '4h': 14400,
            '6h': 21600,
            '8h': 28800,
            '12h': 43200,
            '1d': 86400
        }
        return mapping.get(timeframe, 900)

    async def add_stop_loss_order(self, symbol: str, side: str, stop_price: float,
                                  timeframe: str, quantity: Optional[float] = None) -> int:
        """添加止损订单"""
        # 实时获取持仓以确保数据最新
        positions = await self.binance_client.get_positions()
        position_dict = {pos['symbol']: pos for pos in positions}
        
        # 检查持仓是否存在
        if symbol not in position_dict:
            raise ValueError(f"交易对 {symbol} 没有持仓")
        
        position = position_dict[symbol]
        
        # 验证止损方向
        if position['side'] != side:
            raise ValueError(f"持仓方向不匹配: 持仓为 {position['side']}，止损为 {side}")
        
        # 添加到数据库
        order_id = self.database.add_stop_loss(symbol, side, stop_price, timeframe, quantity)
        
        logger.info(f"添加止损订单成功: ID {order_id}")
        
        return order_id

