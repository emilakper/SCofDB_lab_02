"""
Тест для демонстрации РЕШЕНИЯ проблемы race condition.
Этот тест должен ПРОХОДИТЬ, подтверждая, что при использовании
pay_order_safe() заказ оплачивается только один раз.
"""
import asyncio
import pytest
import uuid
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import text
from app.application.payment_service import PaymentService
from app.domain.exceptions import OrderAlreadyPaidError

# TODO: Настроить подключение к тестовой БД
DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/marketplace"


@pytest.fixture(scope="module")
async def test_engine():
    """
    Создать AsyncEngine для тестов.
    TODO: Реализовать фикстуру:
    1. Создать async engine
    2. Yield engine
    3. Dispose engine после всех тестов
    """
    # TODO: Реализовать создание engine
    engine = create_async_engine(
        DATABASE_URL, 
        echo=False, 
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine):
    """
    Создать сессию БД для тестов.
    TODO: Реализовать фикстуру:
    1. Создать сессию через async engine
    2. Yield сессию
    3. Закрыть сессию после теста
    """
    # TODO: Реализовать создание сессии
    async with AsyncSession(test_engine) as session:
        yield session


@pytest.fixture
async def test_order(test_engine):
    """
    Создать тестовый заказ со статусом 'created'.
    TODO: Реализовать фикстуру:
    1. Создать тестового пользователя
    2. Создать тестовый заказ со статусом 'created'
    3. Записать начальный статус в историю
    4. Вернуть order_id
    5. После теста - очистить данные
    """
    # TODO: Реализовать создание тестового заказа
    user_id = uuid.uuid4()
    order_id = uuid.uuid4()
    
    async with AsyncSession(test_engine) as setup_session:
        async with setup_session.begin():
            # 1. Создать тестового пользователя
            await setup_session.execute(
                text("""
                    INSERT INTO users (id, email, name, created_at)
                    VALUES (:user_id, :email, :name, NOW())
                    ON CONFLICT (id) DO NOTHING
                """),
                {
                    "user_id": user_id,
                    "email": f"test_safe_{order_id}@example.com",
                    "name": "Test User"
                }
            )
            
            # 2. Создать тестовый заказ со статусом 'created'
            await setup_session.execute(
                text("""
                    INSERT INTO orders (id, user_id, status, total_amount, created_at)
                    VALUES (:order_id, :user_id, 'created', 100.00, NOW())
                """),
                {"order_id": order_id, "user_id": user_id}
            )
            
            # 3. Записать начальный статус в историю
            await setup_session.execute(
                text("""
                    INSERT INTO order_status_history (id, order_id, status, changed_at)
                    VALUES (gen_random_uuid(), :order_id, 'created', NOW())
                """),
                {"order_id": order_id}
            )
    
    # 4. Вернуть order_id
    yield order_id
    
    # 5. После теста - очистить данные
    async with AsyncSession(test_engine) as cleanup_session:
        async with cleanup_session.begin():
            await cleanup_session.execute(
                text("DELETE FROM order_status_history WHERE order_id = :order_id"),
                {"order_id": order_id}
            )
            await cleanup_session.execute(
                text("DELETE FROM orders WHERE id = :order_id"),
                {"order_id": order_id}
            )
            await cleanup_session.execute(
                text("DELETE FROM users WHERE id = :user_id"),
                {"user_id": user_id}
            )


@pytest.mark.asyncio
async def test_concurrent_payment_safe_prevents_race_condition(db_session, test_order, test_engine):
    """
    Тест демонстрирует решение проблемы race condition с помощью pay_order_safe().
    ОЖИДАЕМЫЙ РЕЗУЛЬТАТ: Тест ПРОХОДИТ, подтверждая, что заказ был оплачен только один раз.

    TODO: Реализовать тест следующим образом:

    1. Создать два экземпляра PaymentService с РАЗНЫМИ сессиями
       (это имитирует два независимых HTTP-запроса)
       
    2. Запустить два параллельных вызова pay_order_safe():
       
       async def payment_attempt_1():
           service1 = PaymentService(session1)
           return await service1.pay_order_safe(order_id)
           
       async def payment_attempt_2():
           service2 = PaymentService(session2)
           return await service2.pay_order_safe(order_id)
           
       results = await asyncio.gather(
           payment_attempt_1(),
           payment_attempt_2(),
           return_exceptions=True
       )
       
    3. Проверить результаты:
       - Одна попытка должна УСПЕШНО завершиться
       - Вторая попытка должна выбросить OrderAlreadyPaidError
       
    4. Проверить историю оплат:
       
       service = PaymentService(session)
       history = await service.get_payment_history(order_id)
       
       # ОЖИДАЕМ ОДНУ ЗАПИСЬ 'paid' - проблема решена!
       assert len(history) == 1, "Ожидалась 1 запись об оплате (БЕЗ RACE CONDITION!)"
       
    5. Вывести информацию об успешном решении
    """
    # TODO: Реализовать тест, демонстрирующий решение race condition
    
    order_id = test_order
    
    # 1. Функции для параллельных попыток оплаты с НЕЗАВИСИМЫМИ сессиями
    async def payment_attempt_1():
        async with AsyncSession(test_engine) as session1:
            service1 = PaymentService(session1)
            return await service1.pay_order_safe(order_id)
    
    async def payment_attempt_2():
        async with AsyncSession(test_engine) as session2:
            service2 = PaymentService(session2)
            return await service2.pay_order_safe(order_id)
    
    # 2. Запускаем два параллельных вызова
    results = await asyncio.gather(
        payment_attempt_1(),
        payment_attempt_2(),
        return_exceptions=True
    )
    
    # Даем время на завершение всех транзакций
    await asyncio.sleep(0.2)
    
    # 3. Проверить результаты
    success_count = sum(1 for r in results if not isinstance(r, Exception))
    error_count = sum(1 for r in results if isinstance(r, Exception))
    
    assert success_count == 1, f"Ожидалась одна успешная оплата, но получено {success_count}"
    assert error_count == 1, f"Ожидалась одна неудачная попытка, но получено {error_count}"
    
    # 4. Проверить историю оплат (в новой сессии!)
    async with AsyncSession(test_engine) as check_session:
        service = PaymentService(check_session)
        history = await service.get_payment_history(order_id)
    
    # ОЖИДАЕМ ОДНУ ЗАПИСЬ 'paid' - проблема решена!
    assert len(history) == 1, f"Ожидалась 1 запись об оплате (БЕЗ RACE CONDITION!), но получено {len(history)}"
    
    # 5. Вывести информацию об успешном решении
    print(f"\n✅ RACE CONDITION PREVENTED!")
    print(f"Order {order_id} was paid only ONCE:")
    print(f"  - {history[0]['changed_at']}: status = {history[0]['status']}")
    
    # Показать, какая попытка была отклонена
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"Second attempt was rejected: {type(result).__name__}: {result}")

if __name__ == "__main__":
    """
    Запуск теста:
    cd backend
    export PYTHONPATH=$(pwd)
    pytest app/tests/test_concurrent_payment_safe.py -v -s

    ОЖИДАЕМЫЙ РЕЗУЛЬТАТ:
    ✅ test_concurrent_payment_safe_prevents_race_condition PASSED
    """
    pytest.main([__file__, "-v", "-s"])