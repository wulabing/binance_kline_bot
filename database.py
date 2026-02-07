"""
数据库模型和管理模块
用于存储 Telegram Bot 设置的止损信息
"""
import sqlite3
from datetime import datetime
from typing import List, Optional, Dict
import logging

logger = logging.getLogger(__name__)


class StopLossOrder:
    """止损订单数据模型"""
    def __init__(self, id=None, symbol=None, side=None, stop_price=None, 
                 timeframe=None, quantity=None, created_at=None, updated_at=None):
        self.id = id
        self.symbol = symbol  # 交易对，如 BTCUSDT
        self.side = side  # 方向: LONG 或 SHORT
        self.stop_price = stop_price  # 止损价格
        self.timeframe = timeframe  # K线周期: 15m, 1h, 4h
        self.quantity = quantity  # 数量（可选，如果为空则平全部仓位）
        self.created_at = created_at
        self.updated_at = updated_at

    def to_dict(self):
        return {
            'id': self.id,
            'symbol': self.symbol,
            'side': self.side,
            'stop_price': self.stop_price,
            'timeframe': self.timeframe,
            'quantity': self.quantity,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }


class Database:
    """数据库管理类"""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_database()

    def get_connection(self):
        """获取数据库连接（启用 WAL 模式提升并发性能）"""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init_database(self):
        """初始化数据库表"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stop_loss_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    stop_price REAL NOT NULL,
                    timeframe TEXT NOT NULL,
                    quantity REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_symbol ON stop_loss_orders(symbol)
            ''')
            conn.commit()
            logger.info("数据库初始化完成")
        except Exception as e:
            conn.rollback()
            logger.error(f"数据库初始化失败: {e}")
            raise
        finally:
            conn.close()

    def add_stop_loss(self, symbol: str, side: str, stop_price: float,
                     timeframe: str, quantity: Optional[float] = None) -> int:
        """添加止损订单"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO stop_loss_orders (symbol, side, stop_price, timeframe, quantity)
                VALUES (?, ?, ?, ?, ?)
            ''', (symbol, side, stop_price, timeframe, quantity))
            order_id = cursor.lastrowid
            conn.commit()
            logger.info(f"添加止损订单: {symbol} {side} @ {stop_price} [{timeframe}]")
            return order_id
        except Exception as e:
            conn.rollback()
            logger.error(f"添加止损订单失败: {e}")
            raise
        finally:
            conn.close()

    def get_stop_loss_by_id(self, order_id: int) -> Optional[StopLossOrder]:
        """根据ID获取止损订单"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM stop_loss_orders WHERE id = ?', (order_id,))
            row = cursor.fetchone()
            if row:
                return StopLossOrder(
                    id=row['id'], symbol=row['symbol'], side=row['side'],
                    stop_price=row['stop_price'], timeframe=row['timeframe'],
                    quantity=row['quantity'], created_at=row['created_at'],
                    updated_at=row['updated_at']
                )
            return None
        finally:
            conn.close()

    def get_stop_losses_by_symbol(self, symbol: str) -> List[StopLossOrder]:
        """获取指定交易对的所有止损订单"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM stop_loss_orders WHERE symbol = ?', (symbol,))
            rows = cursor.fetchall()
            return [StopLossOrder(
                id=row['id'], symbol=row['symbol'], side=row['side'],
                stop_price=row['stop_price'], timeframe=row['timeframe'],
                quantity=row['quantity'], created_at=row['created_at'],
                updated_at=row['updated_at']
            ) for row in rows]
        finally:
            conn.close()

    def get_all_stop_losses(self) -> List[StopLossOrder]:
        """获取所有止损订单"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM stop_loss_orders ORDER BY created_at DESC')
            rows = cursor.fetchall()
            return [StopLossOrder(
                id=row['id'], symbol=row['symbol'], side=row['side'],
                stop_price=row['stop_price'], timeframe=row['timeframe'],
                quantity=row['quantity'], created_at=row['created_at'],
                updated_at=row['updated_at']
            ) for row in rows]
        finally:
            conn.close()

    def delete_stop_loss(self, order_id: int) -> bool:
        """删除止损订单"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM stop_loss_orders WHERE id = ?', (order_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
            if deleted:
                logger.info(f"删除止损订单: ID {order_id}")
            return deleted
        except Exception as e:
            conn.rollback()
            logger.error(f"删除止损订单失败: {e}")
            raise
        finally:
            conn.close()

    def delete_stop_losses_by_symbol(self, symbol: str) -> int:
        """删除指定交易对的所有止损订单"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM stop_loss_orders WHERE symbol = ?', (symbol,))
            count = cursor.rowcount
            conn.commit()
            if count > 0:
                logger.info(f"删除 {symbol} 的 {count} 个止损订单")
            return count
        except Exception as e:
            conn.rollback()
            logger.error(f"删除止损订单失败: {e}")
            raise
        finally:
            conn.close()

    def update_stop_loss(self, order_id: int, stop_price: Optional[float] = None,
                        timeframe: Optional[str] = None, quantity: Optional[float] = None) -> bool:
        """更新止损订单"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            updates = []
            params = []

            if stop_price is not None:
                updates.append('stop_price = ?')
                params.append(stop_price)
            if timeframe is not None:
                updates.append('timeframe = ?')
                params.append(timeframe)
            if quantity is not None:
                updates.append('quantity = ?')
                params.append(quantity)
            if not updates:
                return False

            updates.append('updated_at = CURRENT_TIMESTAMP')
            params.append(order_id)

            query = f"UPDATE stop_loss_orders SET {', '.join(updates)} WHERE id = ?"
            cursor.execute(query, params)
            updated = cursor.rowcount > 0
            conn.commit()
            if updated:
                logger.info(f"更新止损订单: ID {order_id}")
            return updated
        except Exception as e:
            conn.rollback()
            logger.error(f"更新止损订单失败: {e}")
            raise
        finally:
            conn.close()

