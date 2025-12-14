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
        self.position_cache = {}  # {symbol: position_amt}
        
        # 回调函数
        self.on_position_update = None
        self.on_position_closed = None  # 平仓回调
        self.on_order_update = None
        self.on_account_update = None

    def _generate_signature(self, params: Dict) -> str:
        """生成签名"""
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    async def _request(self, method: str, endpoint: str, signed: bool = False, **kwargs):
        """发送 HTTP 请求"""
        url = f"{self.base_url}{endpoint}"
        headers = {"X-MBX-APIKEY": self.api_key}
        
        if signed:
            params = kwargs.get('params', {})
            params['timestamp'] = int(time.time() * 1000)
            params['signature'] = self._generate_signature(params)
            kwargs['params'] = params
        
        if self.session is None:
            self.session = aiohttp.ClientSession()
        
        async with self.session.request(method, url, headers=headers, **kwargs) as response:
            data = await response.json()
            if response.status != 200:
                logger.error(f"API 请求失败: {data}")
                raise Exception(f"API Error: {data}")
            return data

    async def get_listen_key(self) -> str:
        """获取 User Data Stream 的 listen key"""
        data = await self._request('POST', '/fapi/v1/listenKey')
        self.listen_key = data['listenKey']
        logger.info(f"获取到 Listen Key: {self.listen_key}")
        return self.listen_key

    async def keep_alive_listen_key(self):
        """保持 listen key 活跃（每30分钟调用一次）"""
        while self.running:
            try:
                await asyncio.sleep(1800)  # 30分钟
                if not self.running:
                    break
                await self._request('PUT', '/fapi/v1/listenKey')
                logger.info("Listen Key 已更新")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"更新 Listen Key 失败: {e}")

    async def get_positions(self) -> List[Dict]:
        """获取当前所有持仓"""
        data = await self._request('GET', '/fapi/v2/positionRisk', signed=True)
        
        # 过滤出有持仓的交易对
        positions = []
        for pos in data:
            position_amt = float(pos['positionAmt'])
            if position_amt != 0:
                positions.append({
                    'symbol': pos['symbol'],
                    'side': 'LONG' if position_amt > 0 else 'SHORT',
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

    async def place_market_order(self, symbol: str, side: str, quantity: float) -> Dict:
        """下市价单"""
        params = {
            'symbol': symbol,
            'side': side,  # BUY 或 SELL
            'type': 'MARKET',
            'quantity': quantity
        }
        
        logger.info(f"下市价单: {symbol} {side} {quantity}")
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
        await self.get_listen_key()
        
        # 启动 keep-alive 任务
        asyncio.create_task(self.keep_alive_listen_key())
        
        ws_url = f"{self.ws_base_url}/ws/{self.listen_key}"
        
        while self.running:
            try:
                async with websockets.connect(ws_url) as ws:
                    self.ws_connection = ws
                    logger.info("WebSocket 用户数据流已连接")
                    
                    async for message in ws:
                        if not self.running:
                            break
                        data = json.loads(message)
                        await self._handle_user_data(data)
                        
            except websockets.ConnectionClosed:
                if not self.running:
                    break
                logger.warning("WebSocket 连接断开，5秒后重连...")
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self.running:
                    break
                logger.error(f"WebSocket 错误: {e}")
                await asyncio.sleep(5)
        
        logger.info("WebSocket 用户数据流已停止")

    async def _handle_user_data(self, data: Dict):
        """处理用户数据流消息"""
        event_type = data.get('e')
        
        if event_type == 'ACCOUNT_UPDATE':
            # 账户更新事件
            logger.info(f"账户更新事件: {data}")
            
            # 处理持仓更新
            if 'a' in data and 'P' in data['a']:
                positions = data['a']['P']
                
                # 创建当前持仓快照
                current_positions = {}
                for pos in positions:
                    symbol = pos['s']
                    position_amt = float(pos['pa'])
                    current_positions[symbol] = position_amt
                
                # 检查每个持仓的变化
                all_symbols = set(self.position_cache.keys()) | set(current_positions.keys())
                
                for symbol in all_symbols:
                    old_amt = self.position_cache.get(symbol, 0.0)
                    new_amt = current_positions.get(symbol, 0.0)
                    
                    # 检测平仓：从非0变为0
                    if old_amt != 0 and new_amt == 0:
                        logger.info(f"检测到平仓: {symbol} (从 {old_amt} 变为 0)")
                        if self.on_position_closed:
                            await self.on_position_closed({
                                'symbol': symbol,
                                'previous_side': 'LONG' if old_amt > 0 else 'SHORT',
                                'previous_amount': abs(old_amt)
                            })
                    
                    # 检测开仓或持仓变化：从0变为非0，或数量变化
                    elif new_amt != 0:
                        # 检查是否是新的持仓或持仓数量有变化
                        if old_amt == 0 or abs(old_amt) != abs(new_amt):
                            position_info = {
                                'symbol': symbol,
                                'side': 'LONG' if new_amt > 0 else 'SHORT',
                                'position_amt': abs(new_amt),
                                'entry_price': float(next((p['ep'] for p in positions if p['s'] == symbol), 0)),
                                'unrealized_pnl': float(next((p['up'] for p in positions if p['s'] == symbol), 0))
                            }
                            
                            if self.on_position_update:
                                await self.on_position_update(position_info)
                
                # 更新持仓缓存
                self.position_cache = current_positions.copy()
            
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
                'time': data['E']
            }
            
            logger.info(f"订单更新: {order_info['symbol']} {order_info['side']} {order_info['status']}")
            
            if self.on_order_update:
                await self.on_order_update(order_info)

    async def close(self):
        """关闭连接"""
        self.running = False
        
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
        
        # 等待一小段时间，让后台任务有机会退出
        await asyncio.sleep(0.5)
        
        logger.info("币安客户端已关闭")

