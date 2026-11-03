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
RETENTION_YEARS = 3  # через столько лет после последнего заказа/визита данные удаляются (политика ПДн, разд. 4)
PURGE_INTERVAL = 24 * 3600  # как часто проверять, чьи данные пора удалить
PAYMENT_TTL_HOURS = 24  # неоплаченный заказ аннулируется через сутки (оферта, п. 4.5)
EXPIRE_INTERVAL = 15 * 60  # как часто искать просроченные неоплаченные заказы

# Посылка: одна коробка на заказ; габариты — под 1–2 сложенных худи, тариф считается в основном по весу
BOX_CM = (35, 25, 10)  # длина, ширина, высота
PACKAGING_WEIGHT_G = 150  # коробка, пакет, наполнитель
DEFAULT_ITEM_WEIGHT_G = 500  # если товар уже удалён из каталога, а вес нужен для отправления

STATUS_NEW = "Ожидает оплаты"
STATUS_PAID = "Оплачен"
STATUS_UNPAID = "Отменён: не оплачен"
STATUS_REFUNDED = "Отменён, деньги возвращены"
STATUS_PRESETS = ("Подтверждён", "Отправлен", "Доставлен", "Отменён")
DELIVERY = {"cdek": "СДЭК", "pochta": "Почта России"}
# qr и manager — только чтобы подписать старые заказы в истории; новых таких заказов нет
PAYMENT = {"yookassa": "Онлайн (ЮKassa)", "qr": "QR-код", "manager": "Через менеджера"}
PAYMENT_STATUS = {"pending": "ожидает оплаты", "paid": "оплачен", "cancelled": "платёж отменён",
                  "refunded": "деньги возвращены", "expired": "не оплачен вовремя"}


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
    # ЮKassa, СДЭК, Почта России; пустые ключи — сервис выключен (см. .env.example)
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_vat_code: int | None = None  # None — чек (receipt) в ЮKassa не передаём
    cdek_client_id: str = ""
    cdek_client_secret: str = ""
    cdek_api_url: str = "https://api.cdek.ru/v2"
    cdek_from_city_code: int = 424  # Казань
    cdek_tariff: int = 136  # «Посылка склад-склад»
    cdek_shipment_point: str = ""
    pochta_token: str = ""
    pochta_login: str = ""
    pochta_password: str = ""
    pochta_from_index: str = ""

    @property
    def payments_enabled(self) -> bool:
        return bool(self.yookassa_shop_id and self.yookassa_secret_key)

    @property
    def cdek_enabled(self) -> bool:
        return bool(self.cdek_client_id and self.cdek_client_secret)

    @property
    def pochta_enabled(self) -> bool:
        return bool(self.pochta_token and self.pochta_login and self.pochta_password and self.pochta_from_index)

    @property
    def checkout_enabled(self) -> bool:
        return self.payments_enabled and (self.cdek_enabled or self.pochta_enabled)

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
            yookassa_shop_id=env("YOOKASSA_SHOP_ID"),
            yookassa_secret_key=env("YOOKASSA_SECRET_KEY"),
            yookassa_vat_code=int(env("YOOKASSA_VAT_CODE")) if env("YOOKASSA_VAT_CODE") else None,
            cdek_client_id=env("CDEK_CLIENT_ID"),
            cdek_client_secret=env("CDEK_CLIENT_SECRET"),
            cdek_api_url=env("CDEK_API_URL") or "https://api.cdek.ru/v2",
            cdek_from_city_code=int(env("CDEK_FROM_CITY_CODE") or 424),
            cdek_tariff=int(env("CDEK_TARIFF") or 136),
            cdek_shipment_point=env("CDEK_SHIPMENT_POINT"),
            pochta_token=env("POCHTA_TOKEN"),
            pochta_login=env("POCHTA_LOGIN"),
            pochta_password=env("POCHTA_PASSWORD"),
            pochta_from_index=env("POCHTA_FROM_INDEX"),
        )


def env(name: str) -> str:
    return os.getenv(name, "").strip()
