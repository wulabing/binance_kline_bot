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
    
    def __init__(self, binance_client: BinanceClient, database: Database, enable_evaluation_notification: bool = True):
        self.binance_client = binance_client
        self.database = database
        self.enable_evaluation_notification = enable_evaluation_notification
        
        # 存储每个交易对最新的K线收盘时间
        self.last_kline_close_time = {}
        
        # 回调函数
        self.on_stop_loss_triggered = None
        self.on_evaluation_notification = None
        
        # 监控任务
        self.monitoring_tasks = {}
        
        # 当前持仓缓存
        self.current_positions = {}
        
        # 运行状态
        self.running = False
        
        # 用于按周期分组收集评估信息的字典
        # key: timeframe, value: list of evaluation data
        self.pending_evaluations = {}
        
        # 用于跟踪每个周期是否已经有发送任务在运行
        # key: timeframe, value: bool
        self.evaluation_sending_tasks = {}

        # 后台任务注册表（用于优雅停机）
        self._background_tasks = set()

    def _track_task(self, coro):
        """创建并跟踪后台任务"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def start(self):
        """启动止损管理器"""
        logger.info("启动止损管理器")
        self.running = True
        
        # 立即初始化持仓缓存（避免监控任务启动时缓存为空）
        # 使用 symbol_side 组合作为key，支持双向持仓
        try:
            positions = await self.binance_client.get_positions()
            self.current_positions = {f"{pos['symbol']}_{pos['side']}": pos for pos in positions}
            logger.info(f"止损管理器持仓缓存初始化完成，当前持仓数: {len(positions)}")
        except Exception as e:
            logger.warning(f"初始化止损管理器持仓缓存失败: {e}")
            self.current_positions = {}
        
        # 启动持仓检查任务（纳入生命周期管理）
        self._track_task(self._check_positions_loop())

        # 启动止损监控任务（纳入生命周期管理）
        self._track_task(self._monitor_stop_losses())
    
    async def stop(self):
        """停止止损管理器"""
        logger.info("停止止损管理器")
        self.running = False

        # 取消所有被追踪的后台任务
        if self._background_tasks:
            logger.info(f"正在取消 {len(self._background_tasks)} 个止损监控任务...")
            for task in list(self._background_tasks):
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # 取消所有按交易对分组的监控任务
        if self.monitoring_tasks:
            logger.info(f"正在取消 {len(self.monitoring_tasks)} 个符号监控任务...")
            for key, task in list(self.monitoring_tasks.items()):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                *[t for t in self.monitoring_tasks.values() if not t.done()],
                return_exceptions=True
            )
            self.monitoring_tasks.clear()

        logger.info("止损管理器已完全停止")

    async def _check_positions_loop(self):
        """定期检查持仓，清理已平仓交易对的止损订单"""
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        while self.running:
            try:
                await asyncio.sleep(30)  # 每30秒检查一次
                
                if not self.running:
                    break
                
                try:
                    # 获取当前所有持仓
                    positions = await self.binance_client.get_positions()
                    
                    # 重置错误计数
                    consecutive_errors = 0
                    
                    # 更新持仓缓存（使用 symbol_side 组合作为key，支持双向持仓）
                    self.current_positions = {f"{pos['symbol']}_{pos['side']}": pos for pos in positions}
                    
                    # 获取数据库中所有的止损订单
                    all_stop_losses = self.database.get_all_stop_losses()
                    
                    # 创建当前持仓的key集合（symbol_side组合）
                    current_position_keys = set(self.current_positions.keys())
                    
                    # 收集需要清理的订单（去重，避免同一交易对的多个订单重复处理）
                    orders_to_clean = []
                    for order in all_stop_losses:
                        order_key = f"{order.symbol}_{order.side}"
                        if order_key not in current_position_keys:
                            orders_to_clean.append(order)
                    
                    # 按交易对+方向分组统计
                    cleaned_positions = {}
                    for order in orders_to_clean:
                        order_key = f"{order.symbol}_{order.side}"
                        if order_key not in cleaned_positions:
                            cleaned_positions[order_key] = {'symbol': order.symbol, 'side': order.side, 'count': 0}
                        cleaned_positions[order_key]['count'] += 1
                    
                    # 对每个需要清理的持仓方向，删除订单并发送通知
                    for position_key, info in cleaned_positions.items():
                        # 删除该交易对+方向的所有止损订单
                        # 注意：需要修改数据库删除方法以支持按方向删除
                        deleted_count = 0
                        for order in all_stop_losses:
                            if order.symbol == info['symbol'] and order.side == info['side']:
                                if self.database.delete_stop_loss(order.id):
                                    deleted_count += 1
                        
                        if deleted_count > 0:
                            logger.info(f"清理已平仓持仓 {info['symbol']} {info['side']} 的 {deleted_count} 个止损订单")
                            
                            if self.on_stop_loss_triggered:
                                await self.on_stop_loss_triggered({
                                    'action': 'cleaned',
                                    'symbol': info['symbol'],
                                    'side': info['side'],
                                    'reason': '仓位已不存在',
                                    'deleted_count': deleted_count
                                })
                                
                except Exception as e:
                    consecutive_errors += 1
                    logger.error(f"检查持仓时出错 ({consecutive_errors}/{max_consecutive_errors}): {e}")
                    
                    # 如果连续错误次数过多，等待更长时间
                    if consecutive_errors >= max_consecutive_errors:
                        logger.warning(f"持仓检查连续失败 {consecutive_errors} 次，等待60秒后继续...")
                        await asyncio.sleep(60)
                        consecutive_errors = 0
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"持仓检查循环异常: {e}")

    async def _monitor_stop_losses(self):
        """监控所有止损订单"""
        while self.running:
            try:
                await asyncio.sleep(5)  # 每5秒检查一次
                
                if not self.running:
                    break
                
                # 获取所有止损订单
                all_stop_losses = self.database.get_all_stop_losses()
                
                # 按交易对、时间周期和方向分组（支持双向持仓）
                monitoring_groups = {}
                for order in all_stop_losses:
                    key = f"{order.symbol}_{order.timeframe}_{order.side}"
                    if key not in monitoring_groups:
                        monitoring_groups[key] = []
                    monitoring_groups[key].append(order)

                # 为每个组创建监控任务
                for key, orders in monitoring_groups.items():
                    if key not in self.monitoring_tasks or self.monitoring_tasks[key].done():
                        symbol = orders[0].symbol
                        timeframe = orders[0].timeframe
                        side = orders[0].side
                        task = asyncio.create_task(
                            self._monitor_symbol_timeframe(symbol, timeframe, side)
                        )
                        self.monitoring_tasks[key] = task
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"监控止损订单时出错: {e}")

    async def _monitor_symbol_timeframe(self, symbol: str, timeframe: str, side: str):
        """监控特定交易对、时间周期和方向的止损"""
        logger.info(f"开始监控 {symbol} {side} [{timeframe}] 的止损")
        
        # 转换时间周期为秒数
        interval_seconds = self._timeframe_to_seconds(timeframe)
        
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        while self.running:
            try:
                # 获取该交易对、时间周期和方向的所有止损订单
                all_orders = self.database.get_all_stop_losses()
                orders = [o for o in all_orders
                          if o.symbol == symbol and o.timeframe == timeframe and o.side == side]

                if not orders:
                    # 没有订单了，退出监控
                    logger.info(f"停止监控 {symbol} {side} [{timeframe}]，无止损订单")
                    break

                # 检查是否有持仓（使用 symbol_side 组合检查）
                position_key = f"{symbol}_{side}"
                if position_key not in self.current_positions:
                    logger.info(f"停止监控 {symbol} {side} [{timeframe}]，无持仓")
                    break
                
                try:
                    # 获取最近2根K线：最新的一根可能还在进行中，第二根是已收盘的
                    # 我们应该评估已经完全收盘的K线，而不是正在进行中的K线
                    klines = await self.binance_client.get_kline_data(symbol, timeframe, limit=2)
                    
                    # 重置错误计数
                    consecutive_errors = 0
                    
                    if not klines:
                        await asyncio.sleep(5)
                        continue
                    
                    current_time = await self.binance_client.get_server_time()
                    last_processed_close = self.last_kline_close_time.get(f"{symbol}_{timeframe}", 0)
                    
                    # 找出最近一根已经收盘且未处理过的K线
                    kline_to_check = None
                    
                    for kline in klines:
                        kline_close_time = kline['close_time']
                        
                        # 检查这根K线是否：1) 已收盘  2) 未处理过
                        if current_time >= kline_close_time and kline_close_time > last_processed_close:
                            kline_to_check = kline
                            logger.info(
                                f"{symbol} [{timeframe}] 找到待评估的已收盘K线: "
                                f"开盘时间={datetime.fromtimestamp(kline['open_time']/1000).strftime('%H:%M:%S')}, "
                                f"收盘时间={datetime.fromtimestamp(kline_close_time/1000).strftime('%H:%M:%S')}, "
                                f"收盘价={kline['close']}"
                            )
                            break  # 只处理最近的一根
                    
                    # 处理找到的已收盘K线
                    if kline_to_check:
                        price = kline_to_check['close']
                        check_close_time = kline_to_check['close_time']
                        
                        # 更新最后处理的收盘时间（避免重复处理）
                        self.last_kline_close_time[f"{symbol}_{timeframe}"] = check_close_time
                        
                        # 收集评估信息（如果启用）
                        if self.enable_evaluation_notification:
                            await self._collect_evaluation(symbol, timeframe, price, orders)
                        
                        # 检查每个止损订单
                        for order in orders:
                            await self._check_stop_loss_trigger(order, price)
                    
                    # 等待一段时间再检查
                    await asyncio.sleep(min(10, interval_seconds // 6))
                    
                except Exception as e:
                    consecutive_errors += 1
                    logger.error(f"监控 {symbol} {side} [{timeframe}] 时出错 ({consecutive_errors}/{max_consecutive_errors}): {e}")

                    # 如果连续错误次数过多，等待更长时间
                    if consecutive_errors >= max_consecutive_errors:
                        logger.warning(f"{symbol} {side} [{timeframe}] 监控连续失败 {consecutive_errors} 次，等待30秒后继续...")
                        await asyncio.sleep(30)
                        consecutive_errors = 0
                    else:
                        await asyncio.sleep(5)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self.running:
                    break
                logger.error(f"监控 {symbol} {side} [{timeframe}] 循环异常: {e}")
                await asyncio.sleep(5)

    async def _check_stop_loss_trigger(self, order: StopLossOrder, current_price: float):
        """检查止损是否触发"""
        triggered = False
        
        # 获取持仓信息（使用 symbol_side 组合）
        position_key = f"{order.symbol}_{order.side}"
        position = self.current_positions.get(position_key)
        if not position:
            logger.warning(f"止损订单 {order.id} 对应的持仓不存在: {order.symbol} {order.side}")
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
        """执行止损（确认成交后才删除订单）"""
        try:
            # 确定平仓方向（多头平仓=卖出，空头平仓=买入）
            side = 'SELL' if order.side == 'LONG' else 'BUY'
            position_side = order.side  # LONG 或 SHORT
            quantity = order.quantity if order.quantity else position['position_amt']

            logger.info(
                f"执行止损: {order.symbol} {side} {quantity} "
                f"(持仓方向: {position_side}, 触发价: {trigger_price}, 止损价: {order.stop_price})"
            )

            # 下市价单
            result = await self.binance_client.place_market_order(
                symbol=order.symbol,
                side=side,
                quantity=quantity,
                position_side=position_side
            )

            logger.info(f"止损订单已提交: {result}")

            # 确认订单状态：只有 FILLED 才删除止损记录
            order_status = result.get('status', '')
            if order_status == 'FILLED':
                self.database.delete_stop_loss(order.id)
                logger.info(f"止损订单 {order.id} 已成交，已从数据库删除")
            elif order_status in ('NEW', 'PARTIALLY_FILLED'):
                # 市价单通常立即成交，但极端情况下可能部分成交
                # 仍然删除止损记录，避免重复触发
                self.database.delete_stop_loss(order.id)
                logger.warning(
                    f"止损订单 {order.id} 状态为 {order_status}，"
                    f"已删除止损记录以避免重复触发"
                )
            else:
                # 异常状态（REJECTED/EXPIRED/CANCELED），保留止损记录
                logger.error(
                    f"止损订单 {order.id} 执行异常，状态: {order_status}，"
                    f"保留止损记录以便下次重试"
                )

            # 触发回调
            if self.on_stop_loss_triggered:
                await self.on_stop_loss_triggered({
                    'action': 'executed',
                    'order': order.to_dict(),
                    'trigger_price': trigger_price,
                    'result': result
                })

        except Exception as e:
            logger.error(f"执行止损失败: {e}，保留止损记录以便下次重试")

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

    async def _collect_evaluation(self, symbol: str, timeframe: str, close_price: float, orders: List[StopLossOrder]):
        """收集评估信息"""
        try:
            # 为每个订单评估是否应该执行止损
            evaluations = []
            for order in orders:
                # 获取对应方向的持仓信息（使用 symbol_side 组合）
                position_key = f"{symbol}_{order.side}"
                position = self.current_positions.get(position_key)
                if not position:
                    # 如果持仓不存在，跳过此订单的评估
                    continue
                
                should_trigger = False
                
                # 判断是否应该触发止损
                if order.side == 'LONG':
                    # 多头止损：当前价格 <= 止损价格
                    should_trigger = close_price <= order.stop_price
                else:  # SHORT
                    # 空头止损：当前价格 >= 止损价格
                    should_trigger = close_price >= order.stop_price
                
                evaluations.append({
                    'symbol': symbol,
                    'side': order.side,
                    'close_price': close_price,
                    'stop_price': order.stop_price,
                    'should_trigger': should_trigger,
                    'order_id': order.id
                })
            
            # 如果没有有效的评估信息，直接返回
            if not evaluations:
                return
            
            # 按周期分组存储评估信息
            if timeframe not in self.pending_evaluations:
                self.pending_evaluations[timeframe] = []
            
            # 添加评估信息
            self.pending_evaluations[timeframe].extend(evaluations)
            logger.info(f"收集到 {symbol} [{timeframe}] 的评估信息，当前待发送数量: {len(self.pending_evaluations[timeframe])}")
            
            # 触发发送评估信息（延迟一段时间，以便收集同一周期的多个币种）
            # 如果该周期还没有发送任务在运行，则创建新任务
            if timeframe not in self.evaluation_sending_tasks or not self.evaluation_sending_tasks[timeframe]:
                self.evaluation_sending_tasks[timeframe] = True
                logger.info(f"启动 {timeframe} 周期的评估信息发送任务")
                self._track_task(self._send_evaluation_after_delay(timeframe))
            else:
                logger.info(f"{timeframe} 周期的评估信息发送任务已在运行中，等待合并发送")
            
        except Exception as e:
            logger.error(f"收集评估信息时出错: {e}")
    
    async def _send_evaluation_after_delay(self, timeframe: str):
        """延迟发送评估信息，以便收集同一周期的多个币种"""
        try:
            await asyncio.sleep(8)

            if timeframe not in self.pending_evaluations or not self.pending_evaluations[timeframe]:
                return

            # 取出并清空（原子操作，避免竞态）
            evaluations = self.pending_evaluations[timeframe].copy()
            self.pending_evaluations[timeframe] = []

            if self.on_evaluation_notification and evaluations:
                logger.info(f"发送 {timeframe} 周期的评估信息，包含 {len(evaluations)} 条评估")
                await self.on_evaluation_notification({
                    'timeframe': timeframe,
                    'evaluations': evaluations
                })
        except Exception as e:
            logger.error(f"发送评估信息时出错: {e}")
        finally:
            # 重置标志前检查是否有残留（发送期间新进入的评估）
            has_remaining = (
                timeframe in self.pending_evaluations
                and len(self.pending_evaluations[timeframe]) > 0
            )
            if has_remaining:
                # 有残留，立即启动下一轮发送
                logger.info(f"{timeframe} 发送期间有新评估进入，启动下一轮发送")
                self._track_task(self._send_evaluation_after_delay(timeframe))
            else:
                self.evaluation_sending_tasks[timeframe] = False

    async def add_stop_loss_order(self, symbol: str, side: str, stop_price: float,
                                  timeframe: str, quantity: Optional[float] = None) -> int:
        """添加止损订单"""
        # 实时获取持仓以确保数据最新
        positions = await self.binance_client.get_positions()
        position_dict = {f"{pos['symbol']}_{pos['side']}": pos for pos in positions}
        
        # 检查持仓是否存在（使用 symbol_side 组合）
        position_key = f"{symbol}_{side}"
        if position_key not in position_dict:
            raise ValueError(f"交易对 {symbol} 的 {side} 方向没有持仓")
        
        position = position_dict[position_key]
        
        # 验证止损方向（双向持仓下，side应该直接匹配）
        if position['side'] != side:
            raise ValueError(f"持仓方向不匹配: 持仓为 {position['side']}，止损为 {side}")
        
        # 添加到数据库
        order_id = self.database.add_stop_loss(symbol, side, stop_price, timeframe, quantity)
        
        logger.info(f"添加止损订单成功: ID {order_id}, {symbol} {side} @ {stop_price} [{timeframe}]")
        
        return order_id

