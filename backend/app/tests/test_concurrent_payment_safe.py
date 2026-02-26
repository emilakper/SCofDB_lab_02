"""
Тест для демонстрации РЕШЕНИЯ проблемы race condition.

Этот тест должен ПРОХОДИТЬ, подтверждая, что при использовании
pay_order_safe() заказ оплачивается только один раз.
"""

import asyncio
import pytest
import uuid
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from datetime import datetime

from app.application.payment_service import PaymentService
from app.domain.exceptions import OrderAlreadyPaidError


# TODO: Настроить подключение к тестовой БД
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
async def test_concurrent_payment_safe_prevents_race_condition(db_session, test_order):
    """
    Тест демонстрирует решение проблемы race condition с помощью pay_order_safe().
    
    ОЖИДАЕМЫЙ РЕЗУЛЬТАТ: Тест ПРОХОДИТ, подтверждая, что заказ был оплачен только один раз.
    Это показывает, что метод pay_order_safe() защищен от конкурентных запросов.
    """
    order_id = test_order

    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def payment_attempt_1():
        async with AsyncSessionLocal() as session1:
            service1 = PaymentService(session1)
            return await service1.pay_order_safe(order_id)

    async def payment_attempt_2():
        async with AsyncSessionLocal() as session2:
            service2 = PaymentService(session2)
            return await service2.pay_order_safe(order_id)

    results = await asyncio.gather(
        payment_attempt_1(),
        payment_attempt_2(),
        return_exceptions=True
    )
    await engine.dispose()

    service = PaymentService(db_session)
    history = await service.get_payment_history(order_id)

    success_count = sum(1 for r in results if not isinstance(r, Exception))
    error_count = sum(1 for r in results if isinstance(r, Exception))

    assert success_count == 1, "Ожидалась одна успешная оплата"
    assert error_count == 1, "Ожидалась одна неудачная попытка"
    assert len(history) == 1, "Ожидалась 1 запись об оплате (БЕЗ RACE CONDITION!)"

    print(f"✅ RACE CONDITION PREVENTED!")
    print(f"Order {order_id} was paid only ONCE:")
    print(f"  - {history[0]['changed_at']}: status = {history[0]['status']}")
    print(f"Second attempt was rejected: {results[1] if isinstance(results[1], Exception) else results[0]}")



@pytest.mark.asyncio
async def test_concurrent_payment_safe_with_explicit_timing():
    """
    Дополнительный тест: проверить работу блокировок с явной задержкой.
    """
    order_id = test_order
    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    timestamps = {}
    
    async def slow_payment_attempt():
        """Первая попытка - с задержкой внутри транзакции"""
        attempt_id = "slow"
        async with AsyncSessionLocal() as session:
            try:
                await session.begin()

                service = PaymentService(session)

                result = await session.execute(
                    text("SELECT status FROM orders WHERE id = :id FOR UPDATE"),
                    {"id": order_id}
                )
                order = result.first()
                
                timestamps[f"{attempt_id}_locked"] = datetime.utcnow()

                await asyncio.sleep(1)

                if order and order[0] == 'created':
                    await session.execute(
                        text("UPDATE orders SET status = 'paid' WHERE id = :id"),
                        {"id": order_id}
                    )
                    await session.execute(
                        text("INSERT INTO order_status_history (id, order_id, status) VALUES (gen_random_uuid(), :order_id, 'paid')"),
                        {"order_id": order_id}
                    )
                    await session.commit()
                    timestamps[f"{attempt_id}_committed"] = datetime.utcnow()
                    return "success"
                else:
                    await session.rollback()
                    return "already_paid"
            except Exception as e:
                await session.rollback()
                raise e
    
    async def fast_payment_attempt():
        """Вторая попытка - должна ждать блокировку"""
        attempt_id = "fast"
        async with AsyncSessionLocal() as session:
            try:
                timestamps[f"{attempt_id}_started"] = datetime.utcnow()

                service = PaymentService(session)
                result = await service.pay_order_safe(order_id)
                
                timestamps[f"{attempt_id}_finished"] = datetime.utcnow()
                return result
            except OrderAlreadyPaidError as e:
                timestamps[f"{attempt_id}_error"] = datetime.utcnow()
                raise e
            except Exception as e:
                timestamps[f"{attempt_id}_error"] = datetime.utcnow()
                raise e

    results = await asyncio.gather(
        slow_payment_attempt(),
        fast_payment_attempt(),
        return_exceptions=True
    )
    await engine.dispose()

    slow_result, fast_result = results

    assert isinstance(fast_result, OrderAlreadyPaidError), "Быстрая попытка должна получить ошибку"

    lock_time = timestamps["slow_locked"]
    error_time = timestamps["fast_error"]
    wait_seconds = (error_time - lock_time).total_seconds()

    print(f"\n Результаты теста блокировок:")
    print(f"Медленная транзакция: заблокировала заказ в {lock_time.strftime('%H:%M:%S.%f')[:-3]}")
    print(f"Быстрая транзакция: получила ошибку через {wait_seconds:.2f} сек")
    print(f"Статус: {'Блокировка работает' if wait_seconds >= 0.9 else 'Блокировка не сработала'}")

    assert wait_seconds >= 0.9, f"Быстрая транзакция не ждала блокировку (ждала {wait_seconds:.2f} сек)"


@pytest.mark.asyncio
async def test_concurrent_payment_safe_multiple_orders():
    """
    Дополнительный тест: проверить, что блокировки не мешают разным заказам.
    
    TODO: Реализовать тест:
    1. Создать ДВА разных заказа
    2. Оплатить их ПАРАЛЛЕЛЬНО с помощью pay_order_safe()
    3. Проверить, что ОБА успешно оплачены
    
    Это показывает, что FOR UPDATE блокирует только конкретную строку,
    а не всю таблицу, что важно для производительности.
    """
    user1_id = uuid.uuid4()
    user2_id = uuid.uuid4()
    order1_id = uuid.uuid4()
    order2_id = uuid.uuid4()

    await db_session.execute(
        text("INSERT INTO users (id, email, name) VALUES (:id, :email, :name)"),
        {"id": user1_id, "email": f"user1-{order1_id}@example.com", "name": "User 1"}
    )
    await db_session.execute(
        text("INSERT INTO users (id, email, name) VALUES (:id, :email, :name)"),
        {"id": user2_id, "email": f"user2-{order2_id}@example.com", "name": "User 2"}
    )

    await db_session.execute(
        text("INSERT INTO orders (id, user_id, status, total_amount) VALUES (:id, :user_id, 'created', 99.99)"),
        {"id": order1_id, "user_id": user1_id}
    )
    await db_session.execute(
        text("INSERT INTO orders (id, user_id, status, total_amount) VALUES (:id, :user_id, 'created', 149.99)"),
        {"id": order2_id, "user_id": user2_id}
    )
    
    await db_session.execute(
        text("INSERT INTO order_status_history (id, order_id, status) VALUES (gen_random_uuid(), :order_id, 'created')"),
        {"order_id": order1_id}
    )
    await db_session.execute(
        text("INSERT INTO order_status_history (id, order_id, status) VALUES (gen_random_uuid(), :order_id, 'created')"),
        {"order_id": order2_id}
    )
    await db_session.commit()
    
    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async def pay_order1():
        async with AsyncSessionLocal() as session:
            service = PaymentService(session)
            return await service.pay_order_safe(order1_id)
    
    async def pay_order2():
        async with AsyncSessionLocal() as session:
            service = PaymentService(session)
            return await service.pay_order_safe(order2_id)
    
    results = await asyncio.gather(
        pay_order1(),
        pay_order2(),
        return_exceptions=True
    )
    await engine.dispose()
    
    result1, result2 = results
    
    assert not isinstance(result1, Exception), f"Первый заказ не оплачен: {result1}"
    assert not isinstance(result2, Exception), f"Второй заказ не оплачен: {result2}"
    assert result1 == order1_id, "Первый заказ должен вернуть свой ID"
    assert result2 == order2_id, "Второй заказ должен вернуть свой ID"

    for order_id in [order1_id, order2_id]:
        result = await db_session.execute(
            text("SELECT status FROM orders WHERE id = :id"),
            {"id": order_id}
        )
        status = result.scalar_one()
        assert status == "paid", f"Заказ {order_id} должен быть paid, а он {status}"

        history = await db_session.execute(
            text("SELECT status FROM order_status_history WHERE order_id = :order_id"),
            {"order_id": order_id}
        )
        statuses = history.scalars().all()
        assert len(statuses) == 2, f"Должно быть 2 записи (created и paid), а {len(statuses)}"
        assert "created" in statuses, "Нет записи о создании"
        assert "paid" in statuses, "Нет записи об оплате"
    
    print(f"\n Оба заказа успешно оплачены параллельно:")
    print(f"   - Заказ 1: {order1_id}")
    print(f"   - Заказ 2: {order2_id}")

    await db_session.execute(text("DELETE FROM order_status_history WHERE order_id = :oid"), {"oid": order1_id})
    await db_session.execute(text("DELETE FROM order_status_history WHERE order_id = :oid"), {"oid": order2_id})
    await db_session.execute(text("DELETE FROM orders WHERE id = :oid"), {"oid": order1_id})
    await db_session.execute(text("DELETE FROM orders WHERE id = :oid"), {"oid": order2_id})
    await db_session.commit()

if __name__ == "__main__":
    """
    Запуск теста:
    
    cd backend
    export PYTHONPATH=$(pwd)
    pytest app/tests/test_concurrent_payment_safe.py -v -s
    
    ОЖИДАЕМЫЙ РЕЗУЛЬТАТ:
    ✅ test_concurrent_payment_safe_prevents_race_condition PASSED
    
    Вывод должен показывать:
    ✅ RACE CONDITION PREVENTED!
    Order XXX was paid only ONCE:
      - 2024-XX-XX: status = paid
    Second attempt was rejected: OrderAlreadyPaidError(...)
    """
    pytest.main([__file__, "-v", "-s"])
