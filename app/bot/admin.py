"""Админ-команды: управление заказами кнопками и рассылка. Доступны только ADMIN_CHAT_IDS."""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

from app import texts
from app.bot import keyboards as kb
from app.bot.keyboards import PAGE_SIZE, OrderAct, OrdersPage
from app.config import STATUS_PRESETS, Settings
from app.db import Database, Order
from app.notify import send_safe

log = logging.getLogger(__name__)
router = Router(name="admin")


class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject, settings: Settings) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in settings.admin_ids


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminInput(StatesGroup):
    status = State()
    track = State()


# --- заказы ---

async def render_list(db: Database, page: int) -> tuple[str, object]:
    total = await db.count_orders()
    page = min(max(page, 0), max(0, (total - 1) // PAGE_SIZE))
    orders = await db.list_orders(PAGE_SIZE, page * PAGE_SIZE)
    text = f"<b>📋 Заказы</b> (всего {total})" if total else "Заказов пока нет."
    return text, kb.orders_list(orders, page, total)


async def edit(message: Message, text: str, markup) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


async def notify_customer(bot: Bot, order: Order) -> bool:
    return await send_safe(bot, order.user_id, texts.status_changed(order))


@router.message(Command("edit_offer", "orders"))
async def orders_cmd(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    text, markup = await render_list(db, 0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(OrdersPage.filter())
async def orders_page(call: CallbackQuery, callback_data: OrdersPage, db: Database) -> None:
    text, markup = await render_list(db, callback_data.page)
    await edit(call.message, text, markup)
    await call.answer()


@router.callback_query(OrderAct.filter())
async def order_action(call: CallbackQuery, callback_data: OrderAct, db: Database, bot: Bot, state: FSMContext) -> None:
    cd = callback_data
    order = await db.get_order(cd.id)
    if order is None:
        await call.answer("Заказ не найден — возможно, удалён.", show_alert=True)
        return

    if cd.action == "open":
        await edit(call.message, texts.order_for_admin(order), kb.order_card(order, cd.page))
    elif cd.action == "status" and 0 <= cd.arg < len(STATUS_PRESETS):
        if order.status == STATUS_PRESETS[cd.arg]:
            await call.answer("Этот статус уже установлен")
            return
        order = await db.update_order(order.id, status=STATUS_PRESETS[cd.arg])
        await edit(call.message, texts.order_for_admin(order), kb.order_card(order, cd.page))
        delivered = await notify_customer(bot, order)
        await call.answer("Статус обновлён" if delivered else "Статус обновлён, но покупатель не уведомлён",
                          show_alert=not delivered)
        return
    elif cd.action in ("ask_status", "ask_track"):
        await state.set_state(AdminInput.status if cd.action == "ask_status" else AdminInput.track)
        await state.update_data(order_id=order.id, page=cd.page)
        what = "новый статус" if cd.action == "ask_status" else "трек-номер (или «-», чтобы убрать)"
        await call.message.answer(f"Заказ #{order.id}: отправьте {what}.\n/cancel — отмена.")
    elif cd.action == "delete":
        await edit(call.message, f"Удалить заказ #{order.id} без возможности восстановления?",
                   kb.confirm_delete(order, cd.page))
    elif cd.action == "delete_ok":
        await db.delete_order(order.id)
        text, markup = await render_list(db, cd.page)
        await edit(call.message, f"🗑 Заказ #{order.id} удалён.\n\n{text}", markup)
    await call.answer()


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(AdminInput.status, F.text, ~F.text.startswith("/"))
@router.message(AdminInput.track, F.text, ~F.text.startswith("/"))
async def order_input(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    data = await state.get_data()
    value = message.text.strip()
    if await state.get_state() == AdminInput.status.state:
        if not 1 <= len(value) <= 64:
            await message.answer("Статус — от 1 до 64 символов. Попробуйте ещё раз или /cancel.")
            return
        order = await db.update_order(data["order_id"], status=value)
    else:
        if len(value) > 64:
            await message.answer("Трек-номер слишком длинный. Попробуйте ещё раз или /cancel.")
            return
        order = await db.update_order(data["order_id"], track=None if value == "-" else value)
    await state.clear()
    if order is None:
        await message.answer("Заказ не найден — возможно, удалён.")
        return
    delivered = await notify_customer(bot, order)
    note = "Покупатель уведомлён." if delivered else "⚠️ Покупателя уведомить не удалось."
    await message.answer(f"{texts.order_for_admin(order)}\n\n{note}", reply_markup=kb.order_card(order, data["page"]))


# --- рассылка ---

async def deliver(bot: Bot, chat_id: int, source: Message | None, text: str | None) -> None:
    """Отправляет текст или копию сообщения; один раз ждёт, если Telegram просит паузу."""
    for attempt in range(2):
        try:
            if source:
                await source.copy_to(chat_id)
            else:
                await bot.send_message(chat_id, text, parse_mode=None)
            return
        except TelegramRetryAfter as e:
            if attempt:
                raise
            await asyncio.sleep(e.retry_after)


async def broadcast(message: Message, command: CommandObject, bot: Bot, chat_ids: list[int], db: Database | None):
    source = message.reply_to_message
    if not source and not command.args:
        await message.answer(
            f"Использование: /{command.command} текст — или ответьте этой командой на сообщение, "
            "чтобы разослать его копию (с фото, форматированием и т. д.)."
        )
        return
    await message.answer(f"Старт рассылки. Получателей: {len(chat_ids)}")
    sent = blocked = failed = 0
    for chat_id in chat_ids:
        try:
            await deliver(bot, chat_id, source, command.args)
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            if db and (isinstance(e, TelegramForbiddenError) or "chat not found" in str(e)):
                await db.set_blocked(chat_id)
                blocked += 1
            else:
                failed += 1
                log.warning("Рассылка: %s — %s", chat_id, e)
        except Exception as e:
            failed += 1
            log.warning("Рассылка: %s — %s", chat_id, e)
        await asyncio.sleep(0.05)  # ≤ 20 сообщений в секунду
    await message.answer(f"Рассылка завершена.\nДоставлено: {sent}\nЗаблокировали бота: {blocked}\nОшибки: {failed}")


@router.message(Command("mailing"))
async def mailing(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    await broadcast(message, command, bot, await db.active_user_ids(), db)


@router.message(Command("test_mailing"))
async def test_mailing(message: Message, command: CommandObject, bot: Bot, settings: Settings) -> None:
    await broadcast(message, command, bot, sorted(settings.admin_ids), None)
