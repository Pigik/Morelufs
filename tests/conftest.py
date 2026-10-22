import asyncio
import contextlib
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.api import create_app
from app.catalog import Catalog
from app.config import CATALOG_PATH, ROOT, Settings
from app.db import Database

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
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


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


@contextlib.asynccontextmanager
async def api_client(settings: Settings, bot: FakeBot, login: FakeLogin | None = None):
    db = await Database.open(settings.db_path)
    app = create_app(settings, db, Catalog.load(CATALOG_PATH), bot, login)
    try:
        async with TestClient(TestServer(app)) as client:
            yield client, db
    finally:
        await db.close()


def run(coro):
    return asyncio.run(coro)
