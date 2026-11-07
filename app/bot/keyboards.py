"""Inline-клавиатуры и callback-данные."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import texts
from app.config import STATUS_PRESETS
from app.db import Order

PAGE_SIZE = 8


class OrdersPage(CallbackData, prefix="op"):
    page: int


class OrderAct(CallbackData, prefix="oa"):
    """action: open | status (arg — индекс в STATUS_PRESETS) | ask_status | ask_track | delete | delete_ok |
    delete_comment (удалить с комментарием покупателю) | ship (создать отправление) | fetch_track | refund |
    refund_ok."""

    id: int
    action: str
    page: int = 0
    arg: int = 0


class MailingConfirm(CallbackData, prefix="mc"):
    """Ответ на вопрос «Начать рассылку?»: yes — запустить, no — отменить."""

    action: str


def webapp(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть приложение", web_app=WebAppInfo(url=url))
    ]])


def pay(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оплатить", url=url)]])


def open_order(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Управлять заказом", callback_data=OrderAct(id=order_id, action="open").pack())
    ]])


def orders_list(orders: list[Order], page: int, total: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for o in orders:
        kb.button(text=texts.order_button(o), callback_data=OrderAct(id=o.id, action="open", page=page))
    kb.adjust(1)
    pages = max(1, -(-total // PAGE_SIZE))
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="«", callback_data=OrdersPage(page=page - 1).pack()))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=OrdersPage(page=page).pack()))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(text="»", callback_data=OrdersPage(page=page + 1).pack()))
        kb.row(*nav)
    return kb.as_markup()


def order_card(o: Order, page: int, services: frozenset[str] = frozenset()) -> InlineKeyboardMarkup:
    """services — настроенные сервисы: "payments", "cdek", "pochta". Без сервиса его кнопок нет."""
    kb = InlineKeyboardBuilder()
    paid = o.payment_status == "paid"
    if paid and not o.shipment_id and o.delivery in services:
        kb.button(text="🚚 Создать отправление", callback_data=OrderAct(id=o.id, action="ship", page=page))
    if o.shipment_id and not o.track and o.delivery in services:
        kb.button(text="🔄 Получить трек-номер", callback_data=OrderAct(id=o.id, action="fetch_track", page=page))
    if paid and o.payment_id and "payments" in services:
        kb.button(text="💸 Вернуть деньги", callback_data=OrderAct(id=o.id, action="refund", page=page))
    actions = len(list(kb.buttons))
    for idx, status in enumerate(STATUS_PRESETS):
        mark = "• " if status == o.status else ""
        kb.button(text=f"{mark}{status}", callback_data=OrderAct(id=o.id, action="status", page=page, arg=idx))
    kb.button(text="✏️ Свой статус", callback_data=OrderAct(id=o.id, action="ask_status", page=page))
    kb.button(text="📦 Трек-номер", callback_data=OrderAct(id=o.id, action="ask_track", page=page))
    kb.button(text="🗑 Удалить", callback_data=OrderAct(id=o.id, action="delete", page=page))
    kb.button(text="« К списку", callback_data=OrdersPage(page=page))
    kb.adjust(*[1] * actions, 2)  # действия с сервисами — по одной в строке, остальное по две
    return kb.as_markup()


def confirm_delete(o: Order, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить без комментария", callback_data=OrderAct(id=o.id, action="delete_ok", page=page))
    kb.button(text="Да, удалить с комментарием",
              callback_data=OrderAct(id=o.id, action="delete_comment", page=page))
    kb.button(text="Отмена", callback_data=OrderAct(id=o.id, action="open", page=page))
    kb.adjust(1)
    return kb.as_markup()


def confirm_refund(o: Order, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Да, вернуть {texts.money(o.total)}",
              callback_data=OrderAct(id=o.id, action="refund_ok", page=page))
    kb.button(text="Отмена", callback_data=OrderAct(id=o.id, action="open", page=page))
    return kb.as_markup()


def confirm_mailing() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да", callback_data=MailingConfirm(action="yes").pack()),
        InlineKeyboardButton(text="Нет", callback_data=MailingConfirm(action="no").pack()),
    ]])
