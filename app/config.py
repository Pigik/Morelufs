"""Настройки из окружения и бизнес-константы магазина."""

import os
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = Path(__file__).with_name("catalog.json")

MSK = timezone(timedelta(hours=3))  # в Москве нет перехода на летнее время
RATE_LIMIT_SECONDS = 10  # один заказ в 10 секунд от пользователя
INIT_DATA_TTL = 24 * 3600  # сколько живёт подпись Telegram initData
MAX_CART_ITEMS = 30  # позиций в корзине
MAX_ITEM_QTY = 10  # штук одной позиции
SESSION_DAYS = 30  # сколько живёт вход на сайте
LOGIN_TTL = 600  # секунд на то, чтобы пройти вход в Telegram и вернуться

STATUS_NEW = "Ожидает подтверждения"
STATUS_PRESETS = ("Подтверждён", "Оплачен", "Отправлен", "Доставлен", "Отменён")
DELIVERY = {"cdek": "СДЭК", "pochta": "Почта России"}
PAYMENT = {"qr": "QR-код", "manager": "Через менеджера"}


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    webapp_url: str
    db_path: Path
    web_dir: Path
    host: str
    port: int
    # вход на сайт через Telegram; пустые — на сайте вход выключен, мини-апп работает как раньше
    login_client_id: str = ""
    login_client_secret: str = ""

    @property
    def login_enabled(self) -> bool:
        return bool(self.login_client_id and self.login_client_secret)

    @property
    def login_redirect_uri(self) -> str:
        return self.webapp_url.rstrip("/") + "/auth/callback"

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        admins = frozenset(int(x) for x in os.getenv("ADMIN_CHAT_IDS", "").replace(" ", "").split(",") if x)
        if not token or not admins:
            raise RuntimeError("Задайте TELEGRAM_BOT_TOKEN и ADMIN_CHAT_IDS (см. .env.example)")
        return cls(
            bot_token=token,
            admin_ids=admins,
            webapp_url=os.getenv("WEBAPP_URL", "https://morelufs.ru/"),
            db_path=Path(os.getenv("DB_PATH", "/data/morelufs.db")),
            web_dir=Path(os.getenv("WEB_DIR", ROOT / "web")),
            host=os.getenv("HTTP_HOST", "0.0.0.0"),
            port=int(os.getenv("HTTP_PORT", "8080")),
            login_client_id=os.getenv("TG_LOGIN_CLIENT_ID", "").strip(),
            login_client_secret=os.getenv("TG_LOGIN_CLIENT_SECRET", "").strip(),
        )
