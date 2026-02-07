"""
币安交易所客户端模块
包括 WebSocket 连接、REST API 调用等
"""
import asyncio
import json
import hmac
import hashlib
import time
from typing import Dict, List, Callable, Optional
import websockets
import aiohttp
import logging
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


class BinanceClient:
    """币安交易所客户端"""
    
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        
        if testnet:
            self.base_url = "https://testnet.binancefuture.com"
            self.ws_base_url = "wss://stream.binancefuture.com"
        else:
            self.base_url = "https://fapi.binance.com"
            self.ws_base_url = "wss://fstream.binance.com"
        
        self.listen_key = None
        self.ws_connection = None
        self.session = None
        self.running = False
        
        # 持仓缓存，用于检测持仓变化（开仓/平仓）
        # 双向持仓模式：{symbol_side: position_amt}
        self.position_cache = {}  # {f"{symbol}_{side}": position_amt}
        
        # 订单缓存，用于检测新订单（避免 WebSocket 重连时错过）
        self.order_cache = {}  # {order_id: order_info}
        self.order_cache_lock = asyncio.Lock()  # 保护订单缓存的并发访问
        
        # WebSocket 连接状态
        self.ws_connected = False
        self.last_ws_message_time = 0

        # 后台任务注册表（用于优雅停机）
        self._background_tasks = set()

        # 回调函数
        self.on_position_update = None
        self.on_position_closed = None  # 平仓回调
        self.on_order_update = None
        self.on_account_update = None

    def _track_task(self, coro):
        """创建并跟踪后台任务"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _generate_signature(self, params: Dict) -> str:
        """生成签名"""
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    async def _request(self, method: str, endpoint: str, signed: bool = False, retry_count: int = 3, **kwargs):
        """发送 HTTP 请求，带重试机制"""
        url = f"{self.base_url}{endpoint}"
        headers = {"X-MBX-APIKEY": self.api_key}

        # 保存原始参数的副本，避免重试时签名污染
        original_params = kwargs.get('params', {}).copy()

        for attempt in range(retry_count):
            try:
                if signed:
                    # 每次重试都基于原始参数重新构造，避免旧 signature 被签入
                    params = original_params.copy()
                    params.pop('signature', None)
                    params.pop('timestamp', None)
                    params['timestamp'] = int(time.time() * 1000)
                    params['signature'] = self._generate_signature(params)
                    kwargs['params'] = params

                if self.session is None:
                    # 创建带有超时和连接池配置的 session
                    timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
                    connector = aiohttp.TCPConnector(limit=100, limit_per_host=30, ttl_dns_cache=300)
                    self.session = aiohttp.ClientSession(timeout=timeout, connector=connector)

                async with self.session.request(method, url, headers=headers, **kwargs) as response:
                    data = await response.json()

                    if response.status == 200:
                        return data

                    # 可重试的 HTTP 状态码（429限频、5xx服务端错误）
                    if response.status in (429, 500, 502, 503, 504) and attempt < retry_count - 1:
                        retry_after = int(response.headers.get('Retry-After', 2 ** attempt))
                        logger.warning(f"API 返回 {response.status}，{retry_after}秒后重试...")
                        await asyncio.sleep(retry_after)
                        continue

                    # 不可重试的错误，直接抛出
                    logger.error(f"API 请求失败 [{response.status}]: {data}")
                    raise Exception(f"API Error [{response.status}]: {data}")

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                logger.warning(f"API 请求失败 (尝试 {attempt + 1}/{retry_count}): {e}")
                if attempt < retry_count - 1:
                    # 指数退避：2秒、4秒、8秒
                    wait_time = 2 ** (attempt + 1)
                    logger.info(f"等待 {wait_time} 秒后重试...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"API 请求最终失败，endpoint: {endpoint}")
                    raise

    async def get_server_time(self) -> int:
        """获取币安服务器时间（毫秒时间戳）

        用于校准本地时钟，避免K线收盘判断偏差
        """
        try:
            data = await self._request('GET', '/fapi/v1/time', retry_count=2)
            return data['serverTime']
        except Exception as e:
            logger.warning(f"获取服务器时间失败，回退到本地时间: {e}")
            return int(time.time() * 1000)

    async def get_listen_key(self) -> str:
        """获取 User Data Stream 的 listen key"""
        try:
            data = await self._request('POST', '/fapi/v1/listenKey', retry_count=5)
            self.listen_key = data['listenKey']
            logger.info(f"获取到 Listen Key: {self.listen_key[:8]}...")
            return self.listen_key
        except Exception as e:
            logger.error(f"获取 Listen Key 失败: {e}")
            raise

    async def keep_alive_listen_key(self):
        """保持 listen key 活跃（每30分钟调用一次）"""
        while self.running:
            try:
                await asyncio.sleep(1800)  # 30分钟
                if not self.running:
                    break
                if self.listen_key:
                    try:
                        await self._request('PUT', '/fapi/v1/listenKey', retry_count=3)
                        logger.info("Listen Key 已更新")
                    except Exception as e:
                        logger.error(f"更新 Listen Key 失败: {e}")
                        # 清空 listen_key，让 WebSocket 重连时重新获取
                        self.listen_key = None
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Keep alive 任务错误: {e}")

    async def get_positions(self) -> List[Dict]:
        """获取当前所有持仓
        
        注意：在双向持仓模式下，API会为每个交易对返回LONG和SHORT两条记录
        我们需要过滤出实际有持仓的记录（positionAmt != 0）
        """
        data = await self._request('GET', '/fapi/v2/positionRisk', signed=True)
        
        # 过滤出有持仓的交易对（支持双向持仓）
        positions = []
        for pos in data:
            position_amt = float(pos['positionAmt'])
            # 双向持仓模式下，API会返回每个方向的记录，只保留实际有持仓的
            if position_amt != 0:
                # 获取持仓方向（双向持仓模式下API会直接返回positionSide字段）
                position_side = pos.get('positionSide', 'BOTH')
                
                # 如果API没有返回positionSide（单向持仓模式），根据数量判断
                if position_side == 'BOTH':
                    position_side = 'LONG' if position_amt > 0 else 'SHORT'
                
                positions.append({
                    'symbol': pos['symbol'],
                    'side': position_side,  # LONG 或 SHORT
                    'position_amt': abs(position_amt),
                    'entry_price': float(pos['entryPrice']),
                    'unrealized_pnl': float(pos['unRealizedProfit']),
                    'leverage': int(pos['leverage']),
                    'liquidation_price': float(pos['liquidationPrice'])
                })
        
        return positions

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """获取当前委托订单"""
        params = {}
        if symbol:
            params['symbol'] = symbol
        
        data = await self._request('GET', '/fapi/v1/openOrders', signed=True, params=params)
        
        orders = []
        for order in data:
            orders.append({
                'order_id': order['orderId'],
                'symbol': order['symbol'],
                'side': order['side'],
                'type': order['type'],
                'price': float(order['price']),
                'quantity': float(order['origQty']),
                'status': order['status'],
                'time': order['time'],
                'stop_price': float(order.get('stopPrice', 0)),  # 触发价格
                'reduce_only': order.get('reduceOnly', False)  # 只减仓模式
            })
        
        return orders

    async def place_market_order(self, symbol: str, side: str, quantity: float, 
                                  position_side: Optional[str] = None) -> Dict:
        """下市价单
        
        Args:
            symbol: 交易对
            side: 订单方向 (BUY 或 SELL)
            quantity: 数量
            position_side: 持仓方向 (LONG/SHORT/BOTH)，用于双向持仓模式
        """
        params = {
            'symbol': symbol,
            'side': side,  # BUY 或 SELL
            'type': 'MARKET',
            'quantity': quantity
        }
        
        # 如果指定了持仓方向，添加到参数中（双向持仓模式需要）
        if position_side:
            params['positionSide'] = position_side
        
        logger.info(f"下市价单: {symbol} {side} {quantity} (positionSide={position_side})")
        data = await self._request('POST', '/fapi/v1/order', signed=True, params=params)
        
        return {
            'order_id': data['orderId'],
            'symbol': data['symbol'],
            'side': data['side'],
            'status': data['status'],
            'executed_qty': float(data['executedQty']),
            'price': float(data.get('avgPrice', 0))
        }

    async def get_kline_data(self, symbol: str, interval: str, limit: int = 1) -> List[Dict]:
        """获取K线数据"""
        params = {
            'symbol': symbol,
            'interval': interval,
            'limit': limit
        }
        
        data = await self._request('GET', '/fapi/v1/klines', params=params)
        
        klines = []
        for k in data:
            klines.append({
                'open_time': k[0],
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5]),
                'close_time': k[6]
            })
        
        return klines

    async def start_user_data_stream(self):
        """启动用户数据流 WebSocket"""
        self.running = True
        
        # 启动 keep-alive 任务（纳入生命周期管理）
        self._track_task(self.keep_alive_listen_key())
        
        reconnect_delay = 5  # 初始重连延迟
        max_reconnect_delay = 60  # 最大重连延迟
        
        while self.running:
            try:
                # 获取或刷新 listen key
                if not self.listen_key:
                    await self.get_listen_key()
                
                ws_url = f"{self.ws_base_url}/ws/{self.listen_key}"
                
                # 添加连接超时和心跳配置
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,  # 每20秒发送ping
                    ping_timeout=10,   # ping超时10秒
                    close_timeout=10   # 关闭超时10秒
                ) as ws:
                    self.ws_connection = ws
                    self.ws_connected = True
                    logger.info("WebSocket 用户数据流已连接")
                    
                    # 连接成功，重置重连延迟
                    reconnect_delay = 5
                    
                    # WebSocket 重连后，全量对账（持仓 + 订单）
                    self._track_task(self._reconcile_after_reconnect())
                    
                    async for message in ws:
                        if not self.running:
                            break
                        data = json.loads(message)
                        self.last_ws_message_time = time.time()
                        await self._handle_user_data(data)
                        
            except websockets.ConnectionClosed as e:
                if not self.running:
                    break
                self.ws_connected = False
                logger.warning(f"WebSocket 连接断开 (code: {e.code}, reason: {e.reason})，{reconnect_delay}秒后重连...")
                await asyncio.sleep(reconnect_delay)
                # 指数退避，但不超过最大延迟
                reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)
                
            except asyncio.CancelledError:
                break
                
            except Exception as e:
                if not self.running:
                    break
                self.ws_connected = False
                logger.error(f"WebSocket 错误: {e}", exc_info=True)
                logger.info(f"{reconnect_delay}秒后重连...")
                await asyncio.sleep(reconnect_delay)
                # 指数退避，但不超过最大延迟
                reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)
                
                # 清空 listen_key，下次重连时重新获取
                self.listen_key = None
        
        logger.info("WebSocket 用户数据流已停止")

    async def _reconcile_after_reconnect(self):
        """WebSocket 重连后全量对账（持仓 + 订单）"""
        try:
            await asyncio.sleep(2)
            if not self.ws_connected:
                return

            logger.info("开始 WebSocket 重连后全量对账...")

            # 1. 持仓对账
            await self._reconcile_positions()

            # 2. 订单对账
            await self._check_missed_orders()

            logger.info("WebSocket 重连后全量对账完成")
        except Exception as e:
            logger.error(f"重连后全量对账失败: {e}", exc_info=True)

    async def _reconcile_positions(self):
        """持仓全量对账：REST 快照与缓存比对"""
        try:
            positions = await self.get_positions()
            current_snapshot = {}
            for pos in positions:
                key = f"{pos['symbol']}_{pos['side']}"
                amt = pos['position_amt'] if pos['side'] == 'LONG' else -pos['position_amt']
                current_snapshot[key] = amt

            # 检测缓存中有但实际已平仓的持仓
            for key, old_amt in list(self.position_cache.items()):
                if key not in current_snapshot and old_amt != 0:
                    symbol = key.rsplit('_', 1)[0]
                    side = key.rsplit('_', 1)[1]
                    logger.warning(f"对账发现已平仓: {key} (缓存={old_amt})")
                    del self.position_cache[key]
                    if self.on_position_closed:
                        await self.on_position_closed({
                            'symbol': symbol,
                            'previous_side': side,
                            'previous_amount': abs(old_amt)
                        })

            # 检测实际有但缓存中没有的新持仓
            for key, new_amt in current_snapshot.items():
                if key not in self.position_cache:
                    logger.warning(f"对账发现新持仓: {key} (数量={new_amt})")

            # 更新缓存为最新快照
            self.position_cache = current_snapshot
            logger.info(f"持仓对账完成，当前持仓数: {len(current_snapshot)}")

        except Exception as e:
            logger.error(f"持仓对账失败: {e}", exc_info=True)

    async def _check_missed_orders(self):
        """检查 WebSocket 重连期间是否错过了新订单
        
        在 WebSocket 重连后调用，对比当前的订单列表和缓存，
        如果发现新订单则发送通知
        """
        try:
            # 等待一小段时间，让 WebSocket 稳定
            await asyncio.sleep(2)
            
            if not self.ws_connected:
                return
            
            logger.info("检查 WebSocket 重连期间是否有新订单...")
            
            # 获取当前所有委托订单
            current_orders = await self.get_open_orders()
            
            # 使用锁保护订单缓存的访问，避免与 WebSocket 消息处理并发冲突
            async with self.order_cache_lock:
                # 检查是否有新订单（在缓存中不存在的订单）
                new_orders = []
                for order in current_orders:
                    order_id = order['order_id']
                    if order_id not in self.order_cache:
                        new_orders.append(order)
                        # 更新缓存
                        self.order_cache[order_id] = order
                
                # 清理缓存中已经不存在的订单
                current_order_ids = {order['order_id'] for order in current_orders}
                cached_order_ids = set(self.order_cache.keys())
                closed_order_ids = cached_order_ids - current_order_ids
                
                for order_id in closed_order_ids:
                    del self.order_cache[order_id]
                
                if closed_order_ids:
                    logger.info(f"清理了 {len(closed_order_ids)} 个已完成的订单缓存")
            
            # 在锁外发送通知，避免阻塞其他操作
            if new_orders:
                logger.info(f"发现 {len(new_orders)} 个新订单（WebSocket 重连期间创建）")
                for order in new_orders:
                    if self.on_order_update:
                        # 构建订单信息，格式与 WebSocket 推送一致
                        order_info = {
                            'symbol': order['symbol'],
                            'order_id': order['order_id'],
                            'side': order['side'],
                            'type': order['type'],
                            'status': order['status'],
                            'price': order['price'],
                            'quantity': order['quantity'],
                            'executed_qty': 0.0,  # 从 REST API 无法获取已成交数量，默认为0
                            'stop_price': order.get('stop_price', 0.0),
                            'reduce_only': order.get('reduce_only', False),
                            'time': order['time']
                        }
                        await self.on_order_update(order_info)
            else:
                logger.info("未发现新订单")
                
        except Exception as e:
            logger.error(f"检查错过的订单时出错: {e}", exc_info=True)

    async def _handle_user_data(self, data: Dict):
        """处理用户数据流消息"""
        event_type = data.get('e')
        
        if event_type == 'ACCOUNT_UPDATE':
            # 账户更新事件
            logger.info(f"账户更新事件: {data}")
            
            # 检查事件类型，如果是资金费率支付等不涉及持仓变化的事件，跳过持仓更新
            event_reason = data.get('a', {}).get('m', '')
            if event_reason == 'FUNDING_FEE':
                logger.debug(f"资金费率支付事件，跳过持仓更新")
                if self.on_account_update:
                    await self.on_account_update(data)
                return
            
            # 处理持仓更新
            if 'a' in data and 'P' in data['a']:
                positions = data['a']['P']
                
                # 如果持仓数组为空，说明没有持仓变化，跳过更新
                if not positions:
                    logger.debug(f"持仓数组为空，跳过持仓更新")
                    if self.on_account_update:
                        await self.on_account_update(data)
                    return
                
                # 创建当前持仓快照（只包含本次更新中明确提到的交易对）
                # 支持双向持仓：使用 symbol_side 作为key
                current_positions = {}
                for pos in positions:
                    symbol = pos['s']
                    position_amt = float(pos['pa'])
                    # 获取持仓方向（双向持仓模式下API会返回ps字段）
                    position_side = pos.get('ps', 'BOTH')
                    
                    # 如果是单向持仓模式（BOTH），根据数量判断方向
                    if position_side == 'BOTH':
                        if position_amt > 0:
                            position_side = 'LONG'
                        elif position_amt < 0:
                            position_side = 'SHORT'
                        else:
                            # pa=0 表示平仓，从缓存中推断原方向
                            cached_long = f"{symbol}_LONG"
                            cached_short = f"{symbol}_SHORT"
                            if cached_long in self.position_cache:
                                position_side = 'LONG'
                            elif cached_short in self.position_cache:
                                position_side = 'SHORT'
                            else:
                                # 缓存中也没有，跳过此条
                                logger.debug(f"单向模式 {symbol} pa=0 且缓存无记录，跳过")
                                continue
                    
                    position_key = f"{symbol}_{position_side}"
                    current_positions[position_key] = {
                        'amt': position_amt,
                        'side': position_side,
                        'data': pos
                    }
                
                # 只检查本次更新中明确提到的交易对+方向（避免误判）
                for position_key, pos_info in current_positions.items():
                    old_amt = self.position_cache.get(position_key, 0.0)
                    new_amt = pos_info['amt']
                    position_side = pos_info['side']
                    symbol = position_key.rsplit('_', 1)[0]
                    
                    # 检测平仓：从非0变为0
                    if old_amt != 0 and new_amt == 0:
                        logger.info(f"检测到平仓: {symbol} {position_side} (从 {old_amt} 变为 0)")
                        if self.on_position_closed:
                            await self.on_position_closed({
                                'symbol': symbol,
                                'previous_side': position_side,
                                'previous_amount': abs(old_amt)
                            })
                    
                    # 检测开仓或持仓变化：从0变为非0，或数量变化
                    elif new_amt != 0:
                        # 检查是否是新的持仓或持仓数量有变化
                        if old_amt == 0 or abs(old_amt) != abs(new_amt):
                            pos_data = pos_info['data']
                            position_info = {
                                'symbol': symbol,
                                'side': position_side,
                                'position_amt': abs(new_amt),
                                'entry_price': float(pos_data.get('ep', 0)),
                                'unrealized_pnl': float(pos_data.get('up', 0)),
                                'leverage': int(pos_data.get('lv', 1)),  # 添加杠杆信息
                                'liquidation_price': float(pos_data.get('lp', 0))  # 添加强平价信息
                            }
                            
                            if self.on_position_update:
                                await self.on_position_update(position_info)
                
                # 更新持仓缓存（只更新本次更新中提到的交易对+方向）
                for position_key, pos_info in current_positions.items():
                    position_amt = pos_info['amt']
                    if position_amt == 0:
                        # 如果持仓变为0，从缓存中删除
                        self.position_cache.pop(position_key, None)
                    else:
                        # 否则更新缓存
                        self.position_cache[position_key] = position_amt
            
            if self.on_account_update:
                await self.on_account_update(data)
        
        elif event_type == 'ORDER_TRADE_UPDATE':
            # 订单更新事件
            order = data['o']
            order_info = {
                'symbol': order['s'],
                'order_id': order['i'],
                'side': order['S'],
                'type': order['o'],
                'status': order['X'],
                'price': float(order['p']),
                'quantity': float(order['q']),
                'executed_qty': float(order['z']),
                'stop_price': float(order.get('sp', 0)),  # 添加止损触发价
                'reduce_only': order.get('R', False),  # 添加只减仓标识
                'time': data['E']
            }
            
            logger.info(f"订单更新: {order_info['symbol']} {order_info['side']} {order_info['status']}")
            
            # 使用锁保护订单缓存的访问，避免与 _check_missed_orders() 并发冲突
            order_id = order_info['order_id']
            status = order_info['status']
            should_notify = True
            
            async with self.order_cache_lock:
                if status == 'NEW':
                    # 新订单，检查是否已在缓存中（可能已被 _check_missed_orders() 处理）
                    if order_id in self.order_cache:
                        # 订单已在缓存中，说明已被 _check_missed_orders() 处理过
                        # 不需要重复通知
                        should_notify = False
                        logger.debug(f"订单 {order_id} 已在缓存中，跳过重复通知")
                    else:
                        # 新订单，添加到缓存
                        self.order_cache[order_id] = order_info
                elif status in ['FILLED', 'CANCELED', 'EXPIRED', 'REJECTED']:
                    # 订单已完成，从缓存中删除
                    self.order_cache.pop(order_id, None)
            
            # 只在需要时发送通知
            if should_notify and self.on_order_update:
                await self.on_order_update(order_info)

    async def close(self):
        """关闭连接"""
        self.running = False

        # 取消所有被追踪的后台任务
        if self._background_tasks:
            logger.info(f"正在取消 {len(self._background_tasks)} 个后台任务...")
            for task in list(self._background_tasks):
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        if self.ws_connection:
            try:
                await self.ws_connection.close()
            except Exception as e:
                logger.warning(f"关闭 WebSocket 连接时出错: {e}")

        if self.session:
            try:
                await self.session.close()
            except Exception as e:
                logger.warning(f"关闭 HTTP 会话时出错: {e}")

        logger.info("币安客户端已关闭")
