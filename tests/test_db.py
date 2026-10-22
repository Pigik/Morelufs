from app import texts
from app.db import Database
from tests.conftest import run

ORDER = {"surname": "Иванов", "name": "Иван", "patronymic": "Иванович", "phone": "(900) 123-45-67",
         "telegram": "@ivan", "email": "a@b.c", "delivery": "cdek", "address": "Казань", "payment": "manager",
         "qr_index": None, "comment": "", "total": 2590}
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
        assert (await db.get_user(1)) == {"id": 1, "username": "ivan", "full_name": "", "phone": None}
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
