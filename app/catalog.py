"""Каталог товаров (catalog.json — единственный источник цен) и проверка заказа."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from app.config import BOX_CM, MAX_CART_ITEMS, MAX_ITEM_QTY, PACKAGING_WEIGHT_G
from app.delivery import Parcel

ProductStatus = Literal["in_stock", "preorder", "sold_out"]


class Product(BaseModel):
    id: int
    title: str
    price: int = Field(gt=0)
    old_price: int | None = None
    status: ProductStatus
    sizes: list[str]
    images: list[str]
    # облегчённые версии для миниатюр (-sm) и большие для зума в лайтбоксе (-full).
    # Заполняет Catalog.attach_variants по файлам рядом с оригиналом; без них — те же фото.
    thumbs: list[str] = []
    zoom: list[str] = []
    desc: str = ""
    # граммы, без упаковки; обязательное: без веса служба доставки не посчитает стоимость
    weight: int = Field(gt=0, le=30000)

    @property
    def orderable(self) -> bool:
        return self.status != "sold_out"


def variant(image: str, suffix: str, web_dir: Path) -> str:
    """Путь к версии фото с суффиксом («1-full.webp»), если файл есть рядом с оригиналом."""
    path = Path(image)
    alternative = path.with_name(f"{path.stem}{suffix}{path.suffix}")
    return alternative.as_posix() if (web_dir / alternative).is_file() else image


class Catalog(BaseModel):
    products: list[Product]

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def get(self, product_id: int) -> Product | None:
        return next((p for p in self.products if p.id == product_id), None)

    def attach_variants(self, web_dir: Path) -> None:
        """Миниатюры (-sm) и крупные фото для зума (-full) — по файлам рядом с оригиналом."""
        for product in self.products:
            product.thumbs = [variant(image, "-sm", web_dir) for image in product.images]
            product.zoom = [variant(image, "-full", web_dir) for image in product.images]


class CartLine(BaseModel):
    id: int
    size: str = Field(max_length=10)
    quantity: int = Field(ge=1, le=MAX_ITEM_QTY)


class DeliveryIn(BaseModel):
    """Корзина и доставка: тело POST /api/delivery/quote и часть заказа."""

    model_config = ConfigDict(str_strip_whitespace=True)

    cart: list[CartLine] = Field(min_length=1, max_length=MAX_CART_ITEMS)
    delivery: Literal["cdek", "pochta"]
    cdek_city_code: int | None = Field(default=None, gt=0, lt=10**9)
    cdek_point: str | None = Field(default=None, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    address: str = Field(default="", max_length=150)  # только для Почты; для СДЭК адрес берём у ПВЗ

    @model_validator(mode="after")
    def delivery_target(self) -> "DeliveryIn":
        # PydanticCustomError, а не ValueError: текст уходит покупателю без префикса «Value error»
        if self.delivery == "cdek" and not (self.cdek_city_code and self.cdek_point):
            raise PydanticCustomError("delivery", "Выберите город и пункт выдачи СДЭК")
        if self.delivery == "pochta" and not self.address:
            raise PydanticCustomError("delivery", "Укажите адрес доставки с индексом")
        return self


class OrderIn(DeliveryIn):
    """Тело POST /api/order. Лимиты длины совпадают с maxlength в web/index.html."""

    surname: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=30)
    patronymic: str = Field(min_length=1, max_length=30)
    phone: str = Field(pattern=r"^\(\d{3}\) \d{3}-\d{2}-\d{2}$")
    telegram: str = Field(min_length=1, max_length=33)
    email: str = Field(max_length=100, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    comment: str = Field(default="", max_length=500)
    # галочки в форме: без согласия на обработку ПДн и принятия оферты заказ не создаётся (web/legal/)
    consent: Literal[True]
    offer: Literal[True]


FIELD_NAMES = {
    "surname": "Фамилия", "name": "Имя", "patronymic": "Отчество", "phone": "Телефон",
    "telegram": "Телеграм", "email": "Email", "delivery": "Доставка", "address": "Адрес",
    "cdek_city_code": "Город СДЭК", "cdek_point": "Пункт выдачи СДЭК", "comment": "Комментарий", "cart": "Корзина",
    "consent": "Согласие на обработку персональных данных", "offer": "Согласие с офертой",
}


class OrderError(ValueError):
    """Ошибка, текст которой можно показать покупателю."""


def price_cart(catalog: Catalog, cart: list[CartLine]) -> tuple[list[dict], int]:
    """Сверяет корзину с каталогом. Цены берутся только из каталога, одинаковые позиции склеиваются."""
    merged: dict[tuple[int, str], int] = {}
    for line in cart:
        product = catalog.get(line.id)
        if product is None or not product.orderable:
            raise OrderError("Некоторые товары закончились. Обновите корзину.")
        if line.size not in product.sizes:
            raise OrderError(f"Размер {line.size} недоступен для «{product.title}».")
        merged[(line.id, line.size)] = merged.get((line.id, line.size), 0) + line.quantity

    items = []
    for (product_id, size), qty in merged.items():
        if qty > MAX_ITEM_QTY:
            raise OrderError(f"Не больше {MAX_ITEM_QTY} шт. одной позиции.")
        product = catalog.get(product_id)
        items.append({"product_id": product_id, "title": product.title, "size": size,
                      "qty": qty, "price": product.price})
    return items, sum(i["qty"] * i["price"] for i in items)


def make_parcel(catalog: Catalog, items: list[dict]) -> Parcel:
    """Посылка из позиций price_cart: вес товаров + упаковка, коробка стандартная."""
    weight = sum(i["qty"] * catalog.get(i["product_id"]).weight for i in items) + PACKAGING_WEIGHT_G
    return Parcel(weight, *BOX_CM)


def phone_e164(phone: str) -> str:
    """«(900) 123-45-67» из формы → «+79001234567» для ЮKassa и служб доставки."""
    return "+7" + "".join(ch for ch in phone if ch.isdigit())
