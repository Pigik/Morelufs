"""SQLite (aiosqlite): пользователи, заказы, позиции заказов.

В базе лежат сырые данные; HTML-экранирование делается только при выводе (app/texts.py).
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from app.config import MSK, PAYMENT_TTL_HOURS, STATUS_NEW, STATUS_PAID, STATUS_REFUNDED, STATUS_UNPAID

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
-- вход на сайт через Telegram: незавершённые попытки (state → PKCE) и сессии браузеров
CREATE TABLE IF NOT EXISTS login_attempts (
    state       TEXT PRIMARY KEY,
    verifier    TEXT NOT NULL,
    nonce       TEXT NOT NULL,
    next        TEXT NOT NULL DEFAULT '',          -- экран сайта, куда вернуть после входа
    created_at  INTEGER NOT NULL                   -- unix time
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,                  -- sha256 от cookie: утечка базы не даёт войти
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id);
"""

# Новые колонки старых таблиц: CREATE TABLE IF NOT EXISTS их на рабочей базе не добавит.
# phone — номер, подтверждённый Telegram при входе на сайт;
# bot_dm — может ли бот написать первым: 1 да, 0 нет (не нажимал Start / заблокировал), NULL — ещё не знаем;
# last_seen_at — последний визит (UTC, с точностью до суток): от него и от последнего заказа считается срок хранения;
# legal_version, consent_at — какие редакции документов покупатель принял галочками и когда (доказательство согласия);
# total — товары + доставка; delivery_data — JSON: ПВЗ/код города или почтовый адрес, срок, посылка, веса товаров;
# payment_status — pending | paid | cancelled | refunded | expired (NULL у старых заказов с оплатой по QR/менеджеру);
# shipment_id — uuid заказа СДЭК или id отправления Почты, трек-номер потом пишется в track
COLUMNS = {
    "users": {"phone": "TEXT", "bot_dm": "INTEGER", "last_seen_at": "TEXT"},
    "orders": {"legal_version": "TEXT", "consent_at": "TEXT", "delivery_cost": "INTEGER NOT NULL DEFAULT 0",
               "delivery_data": "TEXT", "payment_id": "TEXT", "payment_url": "TEXT", "payment_status": "TEXT",
               "paid_at": "TEXT", "shipment_id": "TEXT"},
}
# индексы по новым колонкам — после миграции, иначе на старой базе колонки ещё нет
INDEXES = "CREATE INDEX IF NOT EXISTS orders_payment_id ON orders(payment_id);"

ORDER_FIELDS = ("surname", "name", "patronymic", "phone", "telegram", "email", "delivery", "address", "payment",
                "comment", "total", "delivery_cost", "delivery_data", "status", "payment_status")
UPDATABLE = {"status", "track", "payment_id", "payment_url", "payment_status", "shipment_id"}


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
    comment: str
    total: int
    status: str
    track: str | None
    created_at: str
    qr_index: int | None = None  # только у старых заказов с оплатой по QR
    legal_version: str | None = None
    consent_at: str | None = None
    delivery_cost: int = 0
    delivery_data: str | None = None
    payment_id: str | None = None
    payment_url: str | None = None
    payment_status: str | None = None
    paid_at: str | None = None
    shipment_id: str | None = None
    username: str | None = None
    items: list[Item] = field(default_factory=list)

    @property
    def created_msk(self) -> datetime:
        return datetime.fromisoformat(self.created_at + "+00:00").astimezone(MSK)

    @property
    def goods_total(self) -> int:
        return self.total - self.delivery_cost

    @property
    def delivery_info(self) -> dict:
        return json.loads(self.delivery_data) if self.delivery_data else {}

    @property
    def live_payment_url(self) -> str | None:
        """Ссылка ЮKassa, пока заказ ещё можно оплатить (сутки от создания, оферта п. 4.5)."""
        if self.payment_status != "pending" or not self.payment_url:
            return None
        age = datetime.now(UTC) - datetime.fromisoformat(self.created_at + "+00:00")
        return self.payment_url if age < timedelta(hours=PAYMENT_TTL_HOURS) else None


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
        for table, columns in COLUMNS.items():
            existing = {r["name"] for r in await conn.execute_fetchall(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        await conn.executescript(INDEXES)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # --- пользователи ---

    async def upsert_user(self, user_id: int, username: str | None, full_name: str) -> bool:
        """Сохраняет пользователя и снимает флаг блокировки. True, если пользователь новый."""
        cur = await self.conn.execute(
            "INSERT INTO users (id, username, full_name, last_seen_at) VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(id) DO NOTHING",
            (user_id, username, full_name),
        )
        is_new = cur.rowcount == 1
        if not is_new:
            await self.conn.execute(
                "UPDATE users SET username = ?, full_name = ?, is_blocked = 0, last_seen_at = datetime('now') "
                "WHERE id = ?",
                (username, full_name, user_id),
            )
        await self.conn.commit()
        return is_new

    async def touch_user(self, user_id: int) -> None:
        """Отмечает визит. Пишем не чаще раза в сутки: точнее для срока хранения в годах не нужно."""
        cur = await self.conn.execute(
            "UPDATE users SET last_seen_at = datetime('now') WHERE id = ? "
            "AND (last_seen_at IS NULL OR last_seen_at < datetime('now', '-1 day'))", (user_id,))
        if cur.rowcount:
            await self.conn.commit()

    async def forget_user(self, user_id: int) -> int | None:
        """Удаляет все данные человека: заказы с позициями, сессии сайта, сам профиль.

        Возвращает число удалённых заказов или None, если такого пользователя нет.
        """
        rows = await self.conn.execute_fetchall("SELECT 1 FROM users WHERE id = ?", (user_id,))
        cur = await self.conn.execute("DELETE FROM orders WHERE user_id = ?", (user_id,))  # позиции — каскадом
        orders = cur.rowcount
        await self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await self.conn.commit()
        return orders if rows or orders else None

    async def purge_inactive(self, years: int) -> list[int]:
        """Удаляет данные тех, у кого ни заказа, ни визита дольше срока хранения. Возвращает их id."""
        rows = await self.conn.execute_fetchall(
            "SELECT u.id FROM users u WHERE MAX(COALESCE(u.last_seen_at, u.created_at), "
            "COALESCE((SELECT MAX(o.created_at) FROM orders o WHERE o.user_id = u.id), '')) "
            "< datetime('now', ?)", (f"-{int(years)} years",))
        ids = [r["id"] for r in rows]
        for user_id in ids:
            await self.forget_user(user_id)
        return ids

    async def set_blocked(self, user_id: int) -> None:
        await self.conn.execute("UPDATE users SET is_blocked = 1 WHERE id = ?", (user_id,))
        await self.conn.commit()

    async def active_user_ids(self) -> list[int]:
        rows = await self.conn.execute_fetchall("SELECT id FROM users WHERE is_blocked = 0")
        return [r["id"] for r in rows]

    async def get_user(self, user_id: int) -> dict | None:
        rows = await self.conn.execute_fetchall(
            "SELECT id, username, full_name, phone, bot_dm FROM users WHERE id = ?", (user_id,))
        return dict(rows[0]) if rows else None

    async def set_bot_dm(self, user_id: int, reachable: bool) -> None:
        await self.conn.execute("UPDATE users SET bot_dm = ? WHERE id = ?", (int(reachable), user_id))
        await self.conn.commit()

    async def needs_bot_start(self, user_id: int) -> bool:
        """Покупатель с заказами, которому бот не может написать первым: уведомления о заказе не доходят."""
        rows = await self.conn.execute_fetchall(
            "SELECT 1 FROM users u WHERE u.id = ? AND u.bot_dm = 0 "
            "AND EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)", (user_id,))
        return bool(rows)

    async def set_phone(self, user_id: int, phone: str) -> None:
        await self.conn.execute("UPDATE users SET phone = ? WHERE id = ?", (phone, user_id))
        await self.conn.commit()

    # --- вход на сайт ---

    async def add_login_attempt(self, state: str, verifier: str, nonce: str, next_screen: str, now: int,
                                ttl: int) -> None:
        await self.conn.execute("DELETE FROM login_attempts WHERE created_at < ?", (now - ttl,))
        await self.conn.execute(
            "INSERT INTO login_attempts (state, verifier, nonce, next, created_at) VALUES (?, ?, ?, ?, ?)",
            (state, verifier, nonce, next_screen, now),
        )
        await self.conn.commit()

    async def pop_login_attempt(self, state: str, now: int, ttl: int) -> dict | None:
        """Одноразово забирает попытку входа; просроченную не возвращает."""
        rows = await self.conn.execute_fetchall("SELECT * FROM login_attempts WHERE state = ?", (state,))
        await self.conn.execute("DELETE FROM login_attempts WHERE state = ?", (state,))
        await self.conn.commit()
        return dict(rows[0]) if rows and rows[0]["created_at"] >= now - ttl else None

    async def create_session(self, token_hash: str, user_id: int, expires_at: int, now: int) -> None:
        await self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        await self.conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, user_id, expires_at),
        )
        await self.conn.commit()

    async def session_user(self, token_hash: str, now: int) -> dict | None:
        rows = await self.conn.execute_fetchall(
            "SELECT u.id, u.username, u.full_name, u.phone FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at >= ?", (token_hash, now),
        )
        return dict(rows[0]) if rows else None

    async def delete_session(self, token_hash: str) -> None:
        await self.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        await self.conn.commit()

    async def delete_user_sessions(self, user_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await self.conn.commit()
        return cur.rowcount

    # --- заказы ---

    async def create_order(self, user_id: int, data: dict, items: list[dict], legal_version: str | None = None) -> int:
        """legal_version — принятые галочками редакции документов; время согласия ставит база.

        data.total — уже с доставкой; delivery_data можно передать словарём. Статус задаём явно: DEFAULT
        колонки на старой базе — прежний «Ожидает подтверждения».
        """
        data = {"delivery_cost": 0, "delivery_data": None, "status": STATUS_NEW, "payment_status": None, **data}
        if isinstance(data["delivery_data"], dict):
            data["delivery_data"] = json.dumps(data["delivery_data"], ensure_ascii=False)
        cur = await self.conn.execute(
            f"INSERT INTO orders (user_id, {', '.join(ORDER_FIELDS)}, legal_version, consent_at) "
            f"VALUES (?, {', '.join('?' * len(ORDER_FIELDS))}, ?, "
            "CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END)",
            (user_id, *(data[f] for f in ORDER_FIELDS), legal_version, legal_version),
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
        """Меняет поля из UPDATABLE (status, track, ...). Возвращает обновлённый заказ или None, если его нет."""
        assert fields and set(fields) <= UPDATABLE
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = await self.conn.execute(f"UPDATE orders SET {sets} WHERE id = ?", (*fields.values(), order_id))
        await self.conn.commit()
        return await self.get_order(order_id) if cur.rowcount else None

    async def set_payment(self, order_id: int, payment_id: str, url: str) -> Order | None:
        return await self.update_order(order_id, payment_id=payment_id, payment_url=url)

    async def set_shipment(self, order_id: int, shipment_id: str) -> Order | None:
        return await self.update_order(order_id, shipment_id=shipment_id)

    async def order_by_payment(self, payment_id: str) -> Order | None:
        orders = await self._orders("WHERE o.payment_id = ?", (payment_id,))
        return orders[0] if orders else None

    async def _transition(self, order_id: int, sets: str, where: str, params: tuple = ()) -> bool:
        """Условное обновление одним запросом: из двух одновременных уведомлений сработает только одно."""
        cur = await self.conn.execute(f"UPDATE orders SET {sets} WHERE ({where}) AND id = ?", (*params, order_id))
        await self.conn.commit()
        return cur.rowcount == 1

    async def mark_paid(self, order_id: int) -> bool:
        """True только при первой отметке — по нему решаем, уведомлять ли админов.

        Из cancelled/expired тоже: деньги всё-таки пришли (например, повторная попытка картой) — терять их нельзя.
        """
        return await self._transition(
            order_id, "payment_status = 'paid', paid_at = datetime('now'), status = ?",
            "payment_status IS NULL OR payment_status NOT IN ('paid', 'refunded')", (STATUS_PAID,))

    async def close_unpaid(self, order_id: int, payment_status: str = "expired") -> bool:
        """Неоплаченный заказ → отменён (payment_status cancelled/expired). False — его уже оплатили/закрыли."""
        return await self._transition(order_id, "payment_status = ?, status = ?", "payment_status = 'pending'",
                                      (payment_status, STATUS_UNPAID))

    async def mark_refunded(self, order_id: int) -> bool:
        return await self._transition(order_id, "payment_status = 'refunded', status = ?",
                                      "payment_status IS NOT 'refunded'", (STATUS_REFUNDED,))

    async def expired_unpaid(self, hours: int = PAYMENT_TTL_HOURS) -> list[Order]:
        """Неоплаченные онлайн-заказы старше срока оплаты (оферта, п. 4.5)."""
        return await self._orders(
            "WHERE o.payment = 'yookassa' AND o.payment_status = 'pending' AND o.created_at < datetime('now', ?) "
            "ORDER BY o.id", (f"-{int(hours)} hours",))

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
