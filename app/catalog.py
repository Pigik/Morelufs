"""Каталог товаров (catalog.json — единственный источник цен) и проверка заказа."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import MAX_CART_ITEMS, MAX_ITEM_QTY

ProductStatus = Literal["in_stock", "preorder", "sold_out"]


class Product(BaseModel):
    id: int
    title: str
    price: int = Field(gt=0)
    old_price: int | None = None
    status: ProductStatus
    sizes: list[str]
    images: list[str]
    desc: str = ""

    @property
    def orderable(self) -> bool:
        return self.status != "sold_out"


class Catalog(BaseModel):
    payment_qr: list[str]
    products: list[Product]

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def get(self, product_id: int) -> Product | None:
        return next((p for p in self.products if p.id == product_id), None)


class CartLine(BaseModel):
    id: int
    size: str = Field(max_length=10)
    quantity: int = Field(ge=1, le=MAX_ITEM_QTY)


class OrderIn(BaseModel):
    """Тело POST /api/order. Лимиты длины совпадают с maxlength в web/index.html."""

    model_config = ConfigDict(str_strip_whitespace=True)

    surname: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=30)
    patronymic: str = Field(min_length=1, max_length=30)
    phone: str = Field(pattern=r"^\(\d{3}\) \d{3}-\d{2}-\d{2}$")
    telegram: str = Field(min_length=1, max_length=33)
    email: str = Field(max_length=100, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    delivery: Literal["cdek", "pochta"]
    address: str = Field(min_length=1, max_length=150)
    payment: Literal["qr", "manager"]
    comment: str = Field(default="", max_length=500)
    cart: list[CartLine] = Field(min_length=1, max_length=MAX_CART_ITEMS)


FIELD_NAMES = {
    "surname": "Фамилия", "name": "Имя", "patronymic": "Отчество", "phone": "Телефон",
    "telegram": "Телеграм", "email": "Email", "delivery": "Доставка", "address": "Адрес",
    "payment": "Оплата", "comment": "Комментарий", "cart": "Корзина",
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
