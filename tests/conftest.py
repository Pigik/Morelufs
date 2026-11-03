import asyncio
import contextlib
import hashlib
import hmac
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiohttp.test_utils import TestClient, TestServer

from app.api import create_app
from app.catalog import Catalog
from app.config import CATALOG_PATH, ROOT, Settings
from app.db import Database
from app.delivery import City, DeliveryError, Point, PostAddress, Quote
from app.payments import Payment, PaymentError, Refund

TOKEN = "123456:TEST-TOKEN"
ADMINS = frozenset({100, 200})


def make_init_data(user_id: int = 1, token: str = TOKEN, age: int = 0, **user) -> str:
    """initData, подписанная так же, как это делает Telegram."""
    fields = {
        "auth_date": str(int(time.time()) - age),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Иван", "username": "ivan", **user}, ensure_ascii=False),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class FakeBot:
    """unreachable — кому бот не может написать первым (не нажимали Start), как отвечает Telegram."""

    def __init__(self, unreachable=()):
        self.sent: list[tuple[int, str]] = []
        self.markups: list[tuple[int, object]] = []
        self.unreachable = set(unreachable)

    def _check(self, chat_id):
        if chat_id in self.unreachable:
            raise TelegramForbiddenError(method=None, message="Forbidden: bot can't initiate conversation with a user")

    async def send_message(self, chat_id, text, **kwargs):
        self._check(chat_id)
        self.sent.append((chat_id, text))
        self.markups.append((chat_id, kwargs.get("reply_markup")))

    async def send_chat_action(self, chat_id, action, **kwargs):
        self._check(chat_id)

    async def me(self):
        return SimpleNamespace(username="morelufs_test_bot")


@pytest.fixture
def settings(tmp_path) -> Settings:
    # http, а не https: иначе cookie входа будет Secure и тестовый клиент (http) её не отправит
    return Settings(TOKEN, ADMINS, "http://example.test/", tmp_path / "test.db", ROOT / "web", "127.0.0.1", 0)


class FakeLogin:
    """Подмена TelegramLogin: вместо oauth.telegram.org отдаёт заранее заданные claims."""

    def __init__(self, claims: dict | None = None):
        self.claims = claims or {"id": 1, "name": "Иван", "preferred_username": "ivan",
                                 "phone_number": "79001234567", "phone_number_verified": True}
        self.exchanged: list[tuple[str, str, str]] = []

    def authorize_url(self, state: str, challenge: str, nonce: str) -> str:
        return f"https://oauth.telegram.org/auth?state={state}"

    async def exchange(self, code: str, verifier: str, nonce: str) -> dict:
        self.exchanged.append((code, verifier, nonce))
        if code == "bad":
            from app.telegram_login import LoginError
            raise LoginError("bad code")
        return dict(self.claims)


class FakeYooKassa:
    """Подмена YooKassa: платежи pay-1, pay-2…; статус и сумму платежа «у ЮKassa» тест меняет через set()."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.creates: list[dict] = []
        self.payments: dict[str, Payment] = {}
        self.keys: dict[str, str] = {}  # Idempotence-Key → id платежа
        self.refunds: list[tuple[str, int]] = []
        self.refund_status = "succeeded"
        self.closed = False

    async def create(self, **kwargs) -> Payment:
        if self.fail:
            raise PaymentError("ЮKassa недоступна")
        key = kwargs.get("idempotence_key")
        if key in self.keys:
            return self.payments[self.keys[key]]
        self.creates.append(kwargs)
        n = len(self.creates)
        payment = Payment(f"pay-{n}", "pending", kwargs["amount"] * 100, f"https://pay.test/{n}", kwargs["order_id"])
        self.payments[payment.id] = payment
        if key:
            self.keys[key] = payment.id
        return payment

    def set(self, payment_id: str, status: str, amount: int | None = None) -> None:
        """amount — в копейках; None — как при создании."""
        p = self.payments[payment_id]
        self.payments[payment_id] = replace(p, status=status, amount=p.amount if amount is None else amount,
                                            url=p.url if status == "pending" else None)

    async def get(self, payment_id: str) -> Payment:
        if payment_id not in self.payments:
            raise PaymentError("ЮKassa: платёж не найден (not_found)")
        return self.payments[payment_id]

    async def refund(self, payment_id: str, amount: int) -> Refund:
        self.refunds.append((payment_id, amount))
        return Refund(f"ref-{len(self.refunds)}", self.refund_status, payment_id)

    async def get_refund(self, refund_id: str) -> Refund:
        payment_id, _ = self.refunds[int(refund_id.removeprefix("ref-")) - 1]
        return Refund(refund_id, "succeeded", payment_id)

    async def close(self) -> None:
        self.closed = True


KZN_POINT = Point("KZN1", "На Пушкина", "Казань, ул. Пушкина, 1", "Пн-Вс 10-20", 55.79, 49.12)


class FakeCdek:
    def __init__(self):
        self.quotes: list[tuple] = []
        self.shipments: list = []
        self.track: str | None = "1234567890"

    async def cities(self, query: str, limit: int = 10) -> list[City]:
        return [City(424, "Казань", "Республика Татарстан")] if "казань".startswith(query.lower()) else []

    async def points(self, city_code: int) -> list[Point]:
        return [KZN_POINT] if city_code == 424 else []

    async def point(self, city_code: int, code: str) -> Point | None:
        return next((p for p in await self.points(city_code) if p.code == code), None)

    async def quote(self, city_code: int, parcel) -> Quote:
        self.quotes.append((city_code, parcel))
        return Quote(350, 2, 4)

    async def create_shipment(self, shipment) -> str:
        self.shipments.append(shipment)
        return "cdek-uuid-1"

    async def track_number(self, shipment_id: str) -> str | None:
        return self.track

    async def close(self) -> None:
        pass


class FakePochta:
    def __init__(self):
        self.quotes: list[tuple] = []
        self.shipments: list = []

    async def normalize(self, address: str) -> PostAddress:
        if "плохой" in address:
            raise DeliveryError("Почта не распознала адрес. Укажите город, улицу, дом и индекс")
        return PostAddress("420111", "Республика Татарстан", "Казань", "ул. Баумана", "1", "5",
                           full="420111, Республика Татарстан, Казань, ул. Баумана, д. 1, кв. 5")

    async def quote(self, index_to: str, parcel, declared_value: int = 0) -> Quote:
        self.quotes.append((index_to, parcel, declared_value))
        return Quote(410, 3, 6)

    async def create_shipment(self, shipment) -> str:
        self.shipments.append(shipment)
        return "777"

    async def track_number(self, shipment_id: str) -> str | None:
        return None

    async def close(self) -> None:
        pass


ON = object()  # api_client: «создать фейк по умолчанию»; None — сервис выключен


@contextlib.asynccontextmanager
async def api_client(settings: Settings, bot: FakeBot, login: FakeLogin | None = None, payments=ON, cdek=ON,
                     pochta=ON):
    """По умолчанию все сервисы включены фейками; конкретный фейк передайте, чтобы проверить его вызовы."""
    db = await Database.open(settings.db_path)
    app = create_app(settings, db, Catalog.load(CATALOG_PATH), bot, login,
                     payments=FakeYooKassa() if payments is ON else payments,
                     cdek=FakeCdek() if cdek is ON else cdek,
                     pochta=FakePochta() if pochta is ON else pochta)
    try:
        async with TestClient(TestServer(app)) as client:
            yield client, db
    finally:
        await db.close()


def run(coro):
    return asyncio.run(coro)
