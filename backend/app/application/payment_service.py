"""Сервис для демонстрации конкурентных оплат.

Этот модуль содержит два метода оплаты:
1. pay_order_unsafe() - небезопасная реализация (READ COMMITTED без блокировок)
2. pay_order_safe() - безопасная реализация (REPEATABLE READ + FOR UPDATE)
"""

import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import OrderAlreadyPaidError, OrderNotFoundError


class PaymentService:
    """Сервис для обработки платежей с разными уровнями изоляции."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def pay_order_unsafe(self, order_id: uuid.UUID) -> dict:
        """
        НЕБЕЗОПАСНАЯ реализация оплаты заказа.
        
        Использует READ COMMITTED (по умолчанию) без блокировок.
        ЛОМАЕТСЯ при конкурентных запросах - может привести к двойной оплате!
        
        TODO: Реализовать метод следующим образом:
        
        1. Прочитать текущий статус заказа:
           SELECT status FROM orders WHERE id = :order_id
           
        2. Проверить, что статус = 'created'
           Если нет - выбросить OrderAlreadyPaidError
           
        3. Изменить статус на 'paid':
           UPDATE orders SET status = 'paid' 
           WHERE id = :order_id AND status = 'created'
           
        4. Записать изменение в историю:
           INSERT INTO order_status_history (id, order_id, status, changed_at)
           VALUES (gen_random_uuid(), :order_id, 'paid', NOW())
           
        5. Сделать commit
        
        ВАЖНО: НЕ используйте FOR UPDATE!
        ВАЖНО: НЕ меняйте уровень изоляции (оставьте READ COMMITTED по умолчанию)!
        
        Args:
            order_id: ID заказа для оплаты
            
        Returns:
            dict с информацией о заказе после оплаты
            
        Raises:
            OrderNotFoundError: если заказ не найден
            OrderAlreadyPaidError: если заказ уже оплачен
        """
        async with self.session.begin():
            result = await self.session.execute(
                text("SELECT status FROM orders WHERE id = :order_id"),
                {"order_id": order_id}
            )
            status = result.scalar_one_or_none()

            if status is None:
                raise OrderNotFoundError(f"Order {order_id} not found")
            if status != "created":
                raise OrderAlreadyPaidError(order_id)

            await self.session.execute(
                text("UPDATE orders SET status = 'paid' WHERE id = :order_id AND status = 'created'"),
                {"order_id": order_id}
            )

            await self.session.execute(
                text("""
                    INSERT INTO order_status_history (id, order_id, status, changed_at)
                    VALUES (gen_random_uuid(), :order_id, 'paid', NOW())
                """),
                {"order_id": order_id}
            )

        return {"status": "paid", "order_id": str(order_id)}

    async def pay_order_safe(self, order_id: uuid.UUID) -> dict:
        """
        БЕЗОПАСНАЯ реализация оплаты заказа.
        
        Использует REPEATABLE READ + FOR UPDATE для предотвращения race condition.
        Корректно работает при конкурентных запросах.
        
        Args:
            order_id: ID заказа для оплаты
            
        Returns:
            dict с информацией о заказе после оплаты
            
        Raises:
            OrderNotFoundError: если заказ не найден
            OrderAlreadyPaidError: если заказ уже оплачен
        """
        async with self.session.begin():
            await self.session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            )

            result = await self.session.execute(
                text("SELECT status FROM orders WHERE id = :order_id FOR UPDATE"),
                {"order_id": order_id}
            )
            status = result.scalar_one_or_none()

            if status is None:
                raise OrderNotFoundError(f"Order {order_id} not found")
            if status != "created":
                raise OrderAlreadyPaidError(order_id)

            await self.session.execute(
                text("UPDATE orders SET status = 'paid' WHERE id = :order_id AND status = 'created'"),
                {"order_id": order_id}
            )

            await self.session.execute(
                text("""
                    INSERT INTO order_status_history (id, order_id, status, changed_at)
                    VALUES (gen_random_uuid(), :order_id, 'paid', NOW())
                """),
                {"order_id": order_id}
            )

        return {"status": "paid", "order_id": str(order_id)}
    
    async def get_payment_history(self, order_id: uuid.UUID) -> list[dict]:
        """
        Получить историю оплат для заказа.
        
        Используется для проверки, сколько раз был оплачен заказ.
        
        Args:
            order_id: ID заказа
            
        Returns:
            Список словарей с записями об оплате
        """
        result = await self.session.execute(
            text("""
                SELECT id, order_id, status, changed_at
                FROM order_status_history
                WHERE order_id = :order_id AND status = 'paid'
                ORDER BY changed_at
            """),
            {"order_id": order_id}
        )
        return [dict(row._mapping) for row in result]