"""SQLite (aiosqlite): пользователи, заказы, позиции заказов.

В базе лежат сырые данные; HTML-экранирование делается только при выводе (app/texts.py).
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiosqlite

from app.config import MSK, STATUS_NEW

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,               -- Telegram user id
    username    TEXT,
    full_name   TEXT NOT NULL DEFAULT '',
    is_blocked  INTEGER NOT NULL DEFAULT 0,        -- заблокировал бота: пропускаем в рассылке
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    surname     TEXT NOT NULL,
    name        TEXT NOT NULL,
    patronymic  TEXT NOT NULL,
    phone       TEXT NOT NULL,
    telegram    TEXT NOT NULL,
    email       TEXT NOT NULL,
    delivery    TEXT NOT NULL,                     -- ключ config.DELIVERY
    address     TEXT NOT NULL,
    payment     TEXT NOT NULL,                     -- ключ config.PAYMENT
    qr_index    INTEGER,                           -- номер QR-кода (с 1), если оплата по QR
    comment     TEXT NOT NULL DEFAULT '',
    total       INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT '{STATUS_NEW}',
    track       TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS orders_user_id ON orders(user_id);
CREATE TABLE IF NOT EXISTS order_items (
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id  INTEGER NOT NULL,
    title       TEXT NOT NULL,
    size        TEXT NOT NULL,
    qty         INTEGER NOT NULL,
    price       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS order_items_order_id ON order_items(order_id);
"""

ORDER_FIELDS = ("surname", "name", "patronymic", "phone", "telegram", "email",
                "delivery", "address", "payment", "qr_index", "comment", "total")


@dataclass(slots=True)
class Item:
    product_id: int
    title: str
    size: str
    qty: int
    price: int


@dataclass(slots=True)
class Order:
    id: int
    user_id: int
    surname: str
    name: str
    patronymic: str
    phone: str
    telegram: str
    email: str
    delivery: str
    address: str
    payment: str
    qr_index: int | None
    comment: str
    total: int
    status: str
    track: str | None
    created_at: str
    username: str | None = None
    items: list[Item] = field(default_factory=list)

    @property
    def created_msk(self) -> datetime:
        return datetime.fromisoformat(self.created_at + "+00:00").astimezone(MSK)


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: Path | str) -> "Database":
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # --- пользователи ---

    async def upsert_user(self, user_id: int, username: str | None, full_name: str) -> bool:
        """Сохраняет пользователя и снимает флаг блокировки. True, если пользователь новый."""
        cur = await self.conn.execute(
            "INSERT INTO users (id, username, full_name) VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING",
            (user_id, username, full_name),
        )
        is_new = cur.rowcount == 1
        if not is_new:
            await self.conn.execute(
                "UPDATE users SET username = ?, full_name = ?, is_blocked = 0 WHERE id = ?",
                (username, full_name, user_id),
            )
        await self.conn.commit()
        return is_new

    async def set_blocked(self, user_id: int) -> None:
        await self.conn.execute("UPDATE users SET is_blocked = 1 WHERE id = ?", (user_id,))
        await self.conn.commit()

    async def active_user_ids(self) -> list[int]:
        rows = await self.conn.execute_fetchall("SELECT id FROM users WHERE is_blocked = 0")
        return [r["id"] for r in rows]

    # --- заказы ---

    async def create_order(self, user_id: int, data: dict, items: list[dict]) -> int:
        cur = await self.conn.execute(
            f"INSERT INTO orders (user_id, {', '.join(ORDER_FIELDS)}) "
            f"VALUES (?, {', '.join('?' * len(ORDER_FIELDS))})",
            (user_id, *(data[f] for f in ORDER_FIELDS)),
        )
        order_id = cur.lastrowid
        await self.conn.executemany(
            "INSERT INTO order_items (order_id, product_id, title, size, qty, price) "
            "VALUES (:order_id, :product_id, :title, :size, :qty, :price)",
            [{"order_id": order_id, **i} for i in items],
        )
        await self.conn.commit()
        return order_id

    async def get_order(self, order_id: int) -> Order | None:
        orders = await self._orders("WHERE o.id = ?", (order_id,))
        return orders[0] if orders else None

    async def user_orders(self, user_id: int) -> list[Order]:
        return await self._orders("WHERE o.user_id = ? ORDER BY o.id DESC", (user_id,))

    async def list_orders(self, limit: int, offset: int) -> list[Order]:
        return await self._orders("ORDER BY o.id DESC LIMIT ? OFFSET ?", (limit, offset), with_items=False)

    async def count_orders(self) -> int:
        rows = await self.conn.execute_fetchall("SELECT COUNT(*) AS n FROM orders")
        return rows[0]["n"]

    async def update_order(self, order_id: int, **fields: str | None) -> Order | None:
        """Меняет status и/или track. Возвращает обновлённый заказ или None, если его нет."""
        assert fields and set(fields) <= {"status", "track"}
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = await self.conn.execute(f"UPDATE orders SET {sets} WHERE id = ?", (*fields.values(), order_id))
        await self.conn.commit()
        return await self.get_order(order_id) if cur.rowcount else None

    async def delete_order(self, order_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        await self.conn.commit()
        return cur.rowcount == 1

    async def _orders(self, where: str, params: tuple, with_items: bool = True) -> list[Order]:
        rows = await self.conn.execute_fetchall(
            f"SELECT o.*, u.username FROM orders o LEFT JOIN users u ON u.id = o.user_id {where}", params
        )
        orders = [Order(**dict(r)) for r in rows]
        if with_items and orders:
            by_id = {o.id: o for o in orders}
            marks = ", ".join("?" * len(by_id))
            items = await self.conn.execute_fetchall(
                f"SELECT * FROM order_items WHERE order_id IN ({marks}) ORDER BY rowid", tuple(by_id)
            )
            for r in items:
                data = dict(r)
                by_id[data.pop("order_id")].items.append(Item(**data))
        return orders
