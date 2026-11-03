"""Службы доставки: СДЭК (cdek.py) и Почта России (pochta.py). Общие типы — здесь.

Клиенты не знают про заказы и базу: api.py передаёт им посылку (Parcel) и получателя (Recipient).
Деньги — в целых рублях (округление вверх), вес — в граммах, размеры — в сантиметрах.
"""

from dataclasses import dataclass, field


class DeliveryError(Exception):
    """Ошибка, текст которой можно показать покупателю или админу (без технических подробностей)."""


@dataclass(frozen=True, slots=True)
class Parcel:
    weight_g: int
    length_cm: int
    width_cm: int
    height_cm: int


@dataclass(frozen=True, slots=True)
class Quote:
    cost: int  # рубли, округлены вверх
    days_min: int | None = None
    days_max: int | None = None


@dataclass(frozen=True, slots=True)
class City:
    code: int  # код города СДЭК
    name: str
    region: str = ""


@dataclass(frozen=True, slots=True)
class Point:
    """Пункт выдачи СДЭК."""

    code: str
    name: str
    address: str
    work_time: str = ""
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True, slots=True)
class PostAddress:
    """Адрес получателя после нормализации Почтой России."""

    index: str  # шестизначный индекс отделения, где получатель заберёт посылку
    region: str
    place: str  # город / населённый пункт
    street: str = ""
    house: str = ""
    room: str = ""
    area: str = ""  # район
    full: str = ""  # человекочитаемая строка для покупателя и админа


@dataclass(frozen=True, slots=True)
class Item:
    title: str
    size: str
    qty: int
    price: int  # рубли за штуку
    weight_g: int  # вес одной штуки


@dataclass(frozen=True, slots=True)
class Recipient:
    surname: str
    name: str
    patronymic: str
    phone: str  # +79001234567
    email: str


@dataclass(frozen=True, slots=True)
class Shipment:
    """Что нужно службе доставки, чтобы зарегистрировать отправление."""

    order_number: str  # номер заказа магазина, например "ML-15"
    recipient: Recipient
    parcel: Parcel
    items: list[Item] = field(default_factory=list)
    declared_value: int = 0  # объявленная ценность, рубли (стоимость товаров)
    cdek_city_code: int | None = None
    cdek_point: str | None = None
    post_address: PostAddress | None = None
