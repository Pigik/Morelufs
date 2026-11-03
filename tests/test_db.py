from app import legal, texts
from app.db import Database
from tests.conftest import run

ORDER = {"surname": "Иванов", "name": "Иван", "patronymic": "Иванович", "phone": "(900) 123-45-67",
         "telegram": "@ivan", "email": "a@b.c", "delivery": "cdek", "address": "Казань", "payment": "yookassa",
         "comment": "", "total": 2590}
ITEM = {"product_id": 3, "title": "Longsleeve", "size": "M", "qty": 1, "price": 2590}


def test_users_orders_and_texts():
    async def scenario():
        db = await Database.open(":memory:")
        assert await db.upsert_user(1, "ivan", "Иван") is True
        assert await db.upsert_user(1, "ivan2", "Иван") is False
        await db.upsert_user(2, None, "Пётр")
        await db.set_blocked(2)
        assert await db.active_user_ids() == [1]

        order_id = await db.create_order(1, ORDER, [ITEM, {**ITEM, "size": "L"}])
        order = await db.update_order(order_id, status="Отправлен", track="RA123")
        assert (order.status, order.track, order.username, len(order.items)) == ("Отправлен", "RA123", "ivan2", 2)
        assert await db.update_order(999, status="x") is None
        assert "RA123" in texts.order_for_admin(order) and "2 590 руб." in texts.order_for_user(order)

        assert await db.delete_order(order_id) is True
        assert await db.user_orders(1) == []
        rows = await db.conn.execute_fetchall("SELECT COUNT(*) AS n FROM order_items")
        assert rows[0]["n"] == 0  # позиции удалены каскадно
        await db.close()
    run(scenario())


def test_chunks_respect_telegram_limit():
    parts = texts.chunks(["x" * 3000, "y" * 3000, "z" * 10])
    assert len(parts) == 2 and all(len(p) <= texts.TG_LIMIT for p in parts)


def test_sessions_login_attempts_and_migration(tmp_path):
    async def scenario():
        # база «до обновления»: в users нет колонки phone — open() должна её добавить, не потеряв данные
        import aiosqlite
        path = tmp_path / "old.db"
        async with aiosqlite.connect(path) as conn:
            await conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, full_name TEXT NOT NULL "
                               "DEFAULT '', is_blocked INTEGER NOT NULL DEFAULT 0, created_at TEXT)")
            await conn.execute("INSERT INTO users (id, username) VALUES (1, 'ivan')")
            await conn.commit()
        db = await Database.open(path)
        assert (await db.get_user(1)) == {"id": 1, "username": "ivan", "full_name": "", "phone": None, "bot_dm": None}
        await db.set_phone(1, "(900) 123-45-67")

        await db.add_login_attempt("s1", "v", "n", "orders", now=1000, ttl=600)
        assert (await db.pop_login_attempt("s1", now=1100, ttl=600))["next"] == "orders"
        assert await db.pop_login_attempt("s1", now=1100, ttl=600) is None  # одноразовая
        await db.add_login_attempt("s2", "v", "n", "", now=1000, ttl=600)
        assert await db.pop_login_attempt("s2", now=2000, ttl=600) is None  # просрочена

        await db.create_session("h1", 1, expires_at=5000, now=1000)
        assert (await db.session_user("h1", now=4000))["phone"] == "(900) 123-45-67"
        assert await db.session_user("h1", now=6000) is None  # истекла
        assert await db.session_user("h2", now=4000) is None
        assert await db.delete_user_sessions(1) == 1
        await db.close()
    run(scenario())


def test_needs_bot_start_only_with_orders():
    async def scenario():
        db = await Database.open(":memory:")
        await db.upsert_user(1, "ivan", "Иван")
        assert await db.needs_bot_start(1) is False  # ещё не знаем, может ли бот писать
        await db.set_bot_dm(1, False)
        assert await db.needs_bot_start(1) is False  # заказов нет — предупреждать не о чем
        await db.create_order(1, ORDER, [ITEM])
        assert await db.needs_bot_start(1) is True
        await db.set_bot_dm(1, True)
        assert await db.needs_bot_start(1) is False
        assert await db.needs_bot_start(999) is False
        await db.close()
    run(scenario())


def test_forget_and_purge_inactive():
    async def scenario():
        db = await Database.open(":memory:")
        for uid in (1, 2, 3):
            await db.upsert_user(uid, None, f"u{uid}")
        await db.create_order(1, ORDER, [ITEM], legal_version="согласие 01.01.2026")
        await db.create_session("h1", 1, expires_at=10**10, now=0)
        # 2 и 3 давно не заходили; у 3 при этом свежий заказ — его данные ещё нужны
        await db.conn.execute("UPDATE users SET last_seen_at = datetime('now', '-4 years') WHERE id IN (2, 3)")
        await db.create_order(3, ORDER, [ITEM])
        await db.conn.commit()
        assert await db.purge_inactive(3) == [2]
        assert await db.get_user(2) is None and await db.get_user(3) is not None

        await db.conn.execute("UPDATE orders SET created_at = datetime('now', '-4 years') WHERE user_id = 3")
        await db.conn.commit()
        await db.touch_user(3)  # визит раньше суток назад — обновляется
        assert await db.purge_inactive(3) == []
        assert (await db.user_orders(1))[0].legal_version == "согласие 01.01.2026"

        assert await db.forget_user(1) == 1
        assert await db.get_user(1) is None and await db.user_orders(1) == []
        assert await db.session_user("h1", now=1) is None
        rows = await db.conn.execute_fetchall("SELECT COUNT(*) AS n FROM order_items")
        assert rows[0]["n"] == 1  # остались только позиции заказа пользователя 3
        assert await db.forget_user(999) is None
        await db.close()
    run(scenario())


def test_legal_markdown():
    html = legal.to_html("# Заголовок\n\nРедакция от 01.01.2026\nстрока 2\n\n"
                         "- **один** [оферта](/offer)\n- <b>два</b>\n\n"
                         "| A | B |\n|---|---|\n| 1 | https://t.me/x |\n\n[плохо](javascript:alert(1))")
    assert "<h1>Заголовок</h1>" in html and "<p>Редакция от 01.01.2026<br>строка 2</p>" in html
    assert '<li><b>один</b> <a href="/offer">оферта</a></li>' in html and "&lt;b&gt;два" in html
    assert "<th>A</th>" in html and '<td><a href="https://t.me/x">https://t.me/x</a></td>' in html
    assert 'href="javascript' not in html  # чужие схемы ссылок не пропускаем


def test_payment_transitions():
    async def scenario():
        db = await Database.open(":memory:")
        await db.upsert_user(1, "ivan", "Иван")
        data = {**ORDER, "total": 2940, "delivery_cost": 350, "payment_status": "pending",
                "delivery_data": {"cdek_point": "KZN1", "days_min": 2, "days_max": 4}}
        order_id = await db.create_order(1, data, [ITEM])
        await db.set_payment(order_id, "p1", "https://pay.test/1")
        order = await db.order_by_payment("p1")
        assert (order.id, order.status, order.goods_total, order.delivery_info["cdek_point"]) == (
            order_id, "Ожидает оплаты", 2590, "KZN1")
        assert order.live_payment_url == "https://pay.test/1" and await db.order_by_payment("p2") is None
        assert "Товары: 2 590 руб." in texts.order_for_user(order) and "2–4 дн." in texts.order_for_user(order)

        assert await db.mark_paid(order_id) is True
        assert await db.mark_paid(order_id) is False  # повторное уведомление — без второго «оплачен»
        assert await db.close_unpaid(order_id) is False  # оплаченный не отменяется
        order = await db.get_order(order_id)
        assert (order.status, order.payment_status, order.live_payment_url) == ("Оплачен", "paid", None)
        assert (await db.set_shipment(order_id, "uuid")).shipment_id == "uuid"
        assert await db.mark_refunded(order_id) is True and await db.mark_refunded(order_id) is False
        assert await db.mark_paid(order_id) is False  # после возврата «оплачен» не возвращается

        old = await db.create_order(1, data, [ITEM])
        fresh = await db.create_order(1, data, [ITEM])
        await db.conn.execute("UPDATE orders SET created_at = datetime('now', '-25 hours') WHERE id = ?", (old,))
        await db.conn.commit()
        assert [o.id for o in await db.expired_unpaid(24)] == [old]
        assert await db.close_unpaid(old) is True and await db.close_unpaid(old) is False
        assert (await db.get_order(old)).status == "Отменён: не оплачен"
        assert await db.expired_unpaid(24) == [] and (await db.get_order(fresh)).payment_status == "pending"
        await db.close()
    run(scenario())


def test_orders_migration_keeps_old_orders(tmp_path):
    async def scenario():
        # заказы «до онлайн-оплаты»: оплата по QR, без колонок доставки и платежа
        import aiosqlite
        path = tmp_path / "old.db"
        async with aiosqlite.connect(path) as conn:
            await conn.executescript("""
                CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, full_name TEXT NOT NULL DEFAULT '',
                    is_blocked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    surname TEXT NOT NULL, name TEXT NOT NULL, patronymic TEXT NOT NULL, phone TEXT NOT NULL,
                    telegram TEXT NOT NULL,
                    email TEXT NOT NULL, delivery TEXT NOT NULL, address TEXT NOT NULL, payment TEXT NOT NULL,
                    qr_index INTEGER, comment TEXT NOT NULL DEFAULT '', total INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Ожидает подтверждения', track TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO users (id, username) VALUES (1, 'ivan');
                INSERT INTO orders (user_id, surname, name, patronymic, phone, telegram, email, delivery, address,
                    payment, qr_index, total, created_at)
                VALUES (1, 'Иванов', 'Иван', 'Иванович', '(900) 123-45-67', '@ivan', 'a@b.c', 'cdek', 'Казань',
                    'qr', 2, 2590, datetime('now', '-3 days'));
            """)
        db = await Database.open(path)
        old = (await db.user_orders(1))[0]
        assert (old.payment, old.qr_index, old.delivery_cost, old.goods_total, old.payment_status) == (
            "qr", 2, 0, 2590, None)
        assert old.delivery_info == {} and old.live_payment_url is None
        assert "QR-код" in texts.order_for_user(old) and "QR-код:</b> №2" in texts.order_for_admin(old)
        assert await db.expired_unpaid(24) == []  # старые заказы автоотмена не трогает
        new_id = await db.create_order(1, ORDER, [ITEM])
        assert (await db.get_order(new_id)).status == "Ожидает оплаты"  # не DEFAULT старой колонки
        await db.close()
    run(scenario())
