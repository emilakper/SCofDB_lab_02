"""
Тест для демонстрации ПРОБЛЕМЫ race condition.

Этот тест должен ПРОХОДИТЬ, подтверждая, что при использовании
pay_order_unsafe() возникает двойная оплата.
"""

import asyncio
import pytest
import uuid
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from app.application.payment_service import PaymentService

DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/marketplace"


@pytest.fixture
async def db_session():
    """
    Создать сессию БД для тестов.
    """
    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with AsyncSessionLocal() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def test_order(db_session):
    """
    Создать тестовый заказ со статусом 'created'.
    """
    user_id = uuid.uuid4()
    order_id = uuid.uuid4()

    await db_session.execute(
        text("INSERT INTO users (id, email, name) VALUES (:id, :email, :name)"),
        {"id": user_id, "email": f"test-{order_id}@example.com", "name": "Test User"}
    )
    await db_session.execute(
        text("INSERT INTO orders (id, user_id, status, total_amount) VALUES (:id, :user_id, 'created', 99.99)"),
        {"id": order_id, "user_id": user_id}
    )
    await db_session.execute(
        text("INSERT INTO order_status_history (id, order_id, status) VALUES (gen_random_uuid(), :order_id, 'created')"),
        {"order_id": order_id}
    )
    await db_session.commit()

    yield order_id

    await db_session.execute(text("DELETE FROM order_status_history WHERE order_id = :oid"), {"oid": order_id})
    await db_session.execute(text("DELETE FROM orders WHERE id = :oid"), {"oid": order_id})
    await db_session.commit()


@pytest.mark.asyncio
async def test_concurrent_payment_unsafe_demonstrates_race_condition(db_session, test_order):
    """
    Тест демонстрирует проблему race condition при использовании pay_order_unsafe().
    
    ОЖИДАЕМЫЙ РЕЗУЛЬТАТ: Тест ПРОХОДИТ, подтверждая, что заказ был оплачен дважды.
    Это показывает, что метод pay_order_unsafe() НЕ защищен от конкурентных запросов.
    """
    order_id = test_order

    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def payment_attempt_1():
        async with AsyncSessionLocal() as session1:
            service1 = PaymentService(session1)
            return await service1.pay_order_unsafe(order_id)

    async def payment_attempt_2():
        async with AsyncSessionLocal() as session2:
            service2 = PaymentService(session2)
            return await service2.pay_order_unsafe(order_id)

    results = await asyncio.gather(
        payment_attempt_1(),
        payment_attempt_2(),
        return_exceptions=True
    )
    await engine.dispose()

    service = PaymentService(db_session)
    history = await service.get_payment_history(order_id)

    assert len(history) == 2, "Ожидалось 2 записи об оплате (RACE CONDITION!)"

    print(f"⚠️ RACE CONDITION DETECTED!")
    print(f"Order {order_id} was paid TWICE:")
    for record in history:
        print(f"  - {record['changed_at']}: status = {record['status']}")

@pytest.mark.asyncio
async def test_concurrent_payment_unsafe_both_succeed():
    """
    Дополнительный тест: проверить, что ОБЕ транзакции успешно завершились.
    """
    order_id = test_order
    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def payment_attempt_1():
        async with AsyncSessionLocal() as s: 
            return await PaymentService(s).pay_order_unsafe(order_id)
    async def payment_attempt_2():
        async with AsyncSessionLocal() as s: 
            return await PaymentService(s).pay_order_unsafe(order_id)

    results = await asyncio.gather(payment_attempt_1(), payment_attempt_2(), return_exceptions=True)
    await engine.dispose()

    for r in results:
        assert not isinstance(r, Exception), f"Одна из попыток упала: {r}"

if __name__ == "__main__":
    """
    Запуск теста:
    
    cd backend
    export PYTHONPATH=$(pwd)
    pytest app/tests/test_concurrent_payment_unsafe.py -v -s
    
    ОЖИДАЕМЫЙ РЕЗУЛЬТАТ:
    ✅ test_concurrent_payment_unsafe_demonstrates_race_condition PASSED
    
    Вывод должен показывать:
    ⚠️ RACE CONDITION DETECTED!
    Order XXX was paid TWICE:
      - 2024-XX-XX: status = paid
      - 2024-XX-XX: status = paid
    """
    pytest.main([__file__, "-v", "-s"])
