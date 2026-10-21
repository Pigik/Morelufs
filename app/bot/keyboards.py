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
    """action: open | status (arg — индекс в STATUS_PRESETS) | ask_status | ask_track | delete | delete_ok."""

    id: int
    action: str
    page: int = 0
    arg: int = 0


def webapp(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть приложение", web_app=WebAppInfo(url=url))
    ]])


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


def order_card(o: Order, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for idx, status in enumerate(STATUS_PRESETS):
        mark = "• " if status == o.status else ""
        kb.button(text=f"{mark}{status}", callback_data=OrderAct(id=o.id, action="status", page=page, arg=idx))
    kb.button(text="✏️ Свой статус", callback_data=OrderAct(id=o.id, action="ask_status", page=page))
    kb.button(text="📦 Трек-номер", callback_data=OrderAct(id=o.id, action="ask_track", page=page))
    kb.button(text="🗑 Удалить", callback_data=OrderAct(id=o.id, action="delete", page=page))
    kb.button(text="« К списку", callback_data=OrdersPage(page=page))
    kb.adjust(2)
    return kb.as_markup()


def confirm_delete(o: Order, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Да, удалить #{o.id}", callback_data=OrderAct(id=o.id, action="delete_ok", page=page))
    kb.button(text="Отмена", callback_data=OrderAct(id=o.id, action="open", page=page))
    return kb.as_markup()
