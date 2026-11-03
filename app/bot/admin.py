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
from app.catalog import phone_e164
from app.config import BOX_CM, DEFAULT_ITEM_WEIGHT_G, PACKAGING_WEIGHT_G, STATUS_PRESETS, Settings
from app.db import Database, Order
from app.delivery import DeliveryError, Item, Parcel, PostAddress, Recipient, Shipment
from app.notify import send_to_customer
from app.payments import PaymentError

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
    delete_comment = State()


COMMENT_MAX = 1000  # комментарий покупателю при удалении заказа


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


async def notify_customer(bot: Bot, db: Database, order: Order) -> bool:
    return await send_to_customer(bot, db, order.user_id, texts.status_changed(order))


def services(payments=None, cdek=None, pochta=None) -> frozenset[str]:
    """Какие сервисы настроены — от этого зависят кнопки в карточке заказа."""
    return frozenset(k for k, v in {"payments": payments, "cdek": cdek, "pochta": pochta}.items() if v is not None)


async def remove_order(bot: Bot, db: Database, order: Order, comment: str | None = None) -> str:
    """Удаляет заказ и пишет покупателю. Возвращает пометку для админа."""
    if not await db.delete_order(order.id):
        return "Заказ уже удалён."
    log.info("Заказ #%s удалён админом%s", order.id, " с комментарием" if comment else "")
    delivered = await send_to_customer(bot, db, order.user_id, texts.order_deleted(order, comment))
    note = "Покупатель уведомлён." if delivered else "⚠️ Покупателя уведомить не удалось."
    if order.shipment_id:
        note += f"\n⚠️ {texts.shipment_reminder(order)}"
    return note


# --- отправления и возвраты ---

_busy: set[int] = set()  # заказы, по которым идёт запрос к службе: двойное нажатие не создаст два отправления


def shipment_for(o: Order) -> Shipment:
    """Данные для службы доставки — из того, что сохранено при оформлении (delivery_data)."""
    info = o.delivery_info
    weights = info.get("item_weights", {})
    items = [Item(i.title, i.size, i.qty, i.price, weights.get(str(i.product_id), DEFAULT_ITEM_WEIGHT_G))
             for i in o.items]
    parcel = Parcel(**info["parcel"]) if info.get("parcel") else Parcel(
        sum(i.qty * i.weight_g for i in items) + PACKAGING_WEIGHT_G, *BOX_CM)
    post = info.get("post_address")
    return Shipment(
        order_number=f"ML-{o.id}",
        recipient=Recipient(o.surname, o.name, o.patronymic, phone_e164(o.phone), o.email),
        parcel=parcel, items=items, declared_value=info.get("declared_value", o.goods_total),
        cdek_city_code=info.get("cdek_city_code"), cdek_point=info.get("cdek_point"),
        post_address=PostAddress(**post) if post else None,
    )


async def fetch_track(bot: Bot, db: Database, order: Order, client) -> tuple[Order, str]:
    try:
        track = await client.track_number(order.shipment_id)
    except DeliveryError as e:
        return order, f"Трек-номер не получен: {e}"
    if not track:
        return order, "Трек-номер ещё не присвоен — нажмите «Получить трек-номер» через пару минут."
    order = await db.update_order(order.id, track=track)
    delivered = await notify_customer(bot, db, order)
    return order, f"Трек-номер {track}." + ("" if delivered else " Покупателя уведомить не удалось.")


async def create_shipment(bot: Bot, db: Database, order: Order, client) -> tuple[Order, str]:
    """Регистрирует отправление в СДЭК/Почте и сразу пробует получить трек-номер."""
    if order.payment_status != "paid" or order.shipment_id:
        return order, "Отправление уже создано или заказ не оплачен."
    try:
        shipment_id = await client.create_shipment(shipment_for(order))
    except DeliveryError as e:
        return order, f"Отправление не создано: {e}"
    order = await db.set_shipment(order.id, shipment_id)
    log.info("Заказ #%s: создано отправление %s", order.id, shipment_id)
    order, note = await fetch_track(bot, db, order, client)
    return order, f"Отправление создано. {note}"


async def refund(bot: Bot, db: Database, order: Order, payments) -> tuple[Order, str]:
    """Полный возврат через ЮKassa и отмена заказа."""
    if order.payment_status != "paid" or not order.payment_id:
        return order, "Заказ не оплачен онлайн — возвращать нечего."
    try:
        result = await payments.refund(order.payment_id, order.total)
    except PaymentError as e:
        return order, f"ЮKassa не вернула деньги: {e}"
    if result.status == "pending":  # дождёмся уведомления refund.succeeded
        log.info("Возврат по заказу #%s в обработке (%s)", order.id, result.id)
        return order, ("Возврат принят ЮKassa и обрабатывается. Когда он пройдёт, заказ отменится "
                       "и покупатель получит уведомление.")
    if result.status != "succeeded":
        log.warning("Возврат по заказу #%s: статус %s", order.id, result.status)
        return order, f"ЮKassa отклонила возврат (статус {result.status}). Проверьте платёж в личном кабинете ЮKassa."
    if await db.mark_refunded(order.id):
        order = await db.get_order(order.id)
        log.info("Заказ #%s: деньги возвращены", order.id)
        await send_to_customer(bot, db, order.user_id, texts.refunded(order))
    return order, "Деньги возвращены, покупатель уведомлён."


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
async def order_action(call: CallbackQuery, callback_data: OrderAct, db: Database, bot: Bot, state: FSMContext,
                       payments=None, cdek=None, pochta=None) -> None:
    cd = callback_data
    order = await db.get_order(cd.id)
    if order is None:
        await call.answer("Заказ не найден — возможно, удалён.", show_alert=True)
        return
    svc = services(payments, cdek, pochta)

    if cd.action == "open":
        await edit(call.message, texts.order_for_admin(order), kb.order_card(order, cd.page, svc))
    elif cd.action in ("ship", "fetch_track", "refund_ok"):
        client = payments if cd.action == "refund_ok" else {"cdek": cdek, "pochta": pochta}.get(order.delivery)
        if client is None:
            await call.answer("Сервис не настроен: нет ключей в .env.", show_alert=True)
            return
        if order.id in _busy:
            await call.answer("Запрос по этому заказу уже выполняется…")
            return
        _busy.add(order.id)
        try:
            if cd.action == "ship":
                order, note = await create_shipment(bot, db, order, client)
            elif cd.action == "fetch_track":
                order, note = await fetch_track(bot, db, order, client)
            else:
                order, note = await refund(bot, db, order, client)
        finally:
            _busy.discard(cd.id)
        await edit(call.message, texts.order_for_admin(order), kb.order_card(order, cd.page, svc))
        await call.answer(note[:200], show_alert=True)  # 200 — лимит Telegram для всплывающего ответа
        return
    elif cd.action == "refund":
        await edit(call.message, f"Вернуть покупателю {texts.money(order.total)} по заказу #{order.id} и отменить "
                   "заказ? Если посылка уже передана в доставку — сначала отзовите её.",
                   kb.confirm_refund(order, cd.page))
    elif cd.action == "status" and 0 <= cd.arg < len(STATUS_PRESETS):
        if order.status == STATUS_PRESETS[cd.arg]:
            await call.answer("Этот статус уже установлен")
            return
        order = await db.update_order(order.id, status=STATUS_PRESETS[cd.arg])
        await edit(call.message, texts.order_for_admin(order), kb.order_card(order, cd.page, svc))
        delivered = await notify_customer(bot, db, order)
        await call.answer("Статус обновлён" if delivered else "Статус обновлён, но покупатель не уведомлён",
                          show_alert=not delivered)
        return
    elif cd.action in ("ask_status", "ask_track"):
        await state.set_state(AdminInput.status if cd.action == "ask_status" else AdminInput.track)
        await state.update_data(order_id=order.id, page=cd.page)
        what = "новый статус" if cd.action == "ask_status" else "трек-номер (или «-», чтобы убрать)"
        await call.message.answer(f"Заказ #{order.id}: отправьте {what}.\n/cancel — отмена.")
    elif cd.action == "delete":
        await edit(call.message, texts.delete_prompt(order), kb.confirm_delete(order, cd.page))
    elif cd.action == "delete_ok":
        note = await remove_order(bot, db, order)
        text, markup = await render_list(db, cd.page)
        await edit(call.message, f"🗑 Заказ #{order.id} удалён. {note}\n\n{text}", markup)
    elif cd.action == "delete_comment":
        await state.set_state(AdminInput.delete_comment)
        await state.update_data(order_id=order.id, page=cd.page)
        await call.message.answer(f"Заказ #{order.id}: отправьте комментарий для покупателя — он придёт вместе "
                                  f"с сообщением об удалении.\n/cancel — отмена, заказ останется.")
    await call.answer()


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(AdminInput.status, F.text, ~F.text.startswith("/"))
@router.message(AdminInput.track, F.text, ~F.text.startswith("/"))
async def order_input(message: Message, state: FSMContext, db: Database, bot: Bot, payments=None, cdek=None,
                      pochta=None) -> None:
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
    delivered = await notify_customer(bot, db, order)
    note = "Покупатель уведомлён." if delivered else "⚠️ Покупателя уведомить не удалось."
    markup = kb.order_card(order, data["page"], services(payments, cdek, pochta))
    await message.answer(f"{texts.order_for_admin(order)}\n\n{note}", reply_markup=markup)


@router.message(AdminInput.delete_comment, F.text, ~F.text.startswith("/"))
async def delete_comment_input(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    data = await state.get_data()
    comment = message.text.strip()
    if not 1 <= len(comment) <= COMMENT_MAX:
        await message.answer(f"Комментарий — от 1 до {COMMENT_MAX} символов. Попробуйте ещё раз или /cancel.")
        return
    await state.clear()
    order = await db.get_order(data["order_id"])
    if order is None:
        await message.answer("Заказ не найден — возможно, уже удалён.")
        return
    note = await remove_order(bot, db, order, comment)
    text, markup = await render_list(db, data["page"])
    await message.answer(f"🗑 Заказ #{order.id} удалён. {note}\n\n{text}", reply_markup=markup)


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
            "чтобы разослать его копию (с фото, форматированием и т. д.).\n\n"
            "Только служебные сообщения (о работе магазина, заказах, доставке). Реклама — новинки, скидки, "
            "розыгрыши — требует отдельного согласия получателей (ст. 18 закона «О рекламе»), его у них нет."
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


# --- удаление персональных данных ---

@router.message(Command("forget"))
async def forget(message: Message, command: CommandObject, db: Database) -> None:
    """Отзыв согласия или запрос на удаление: стираем заказы, сессии и профиль человека (политика ПДн, разд. 8)."""
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer(
            "Использование: /forget <i>telegram_id</i> — удалить все данные покупателя: заказы, вход на сайт, "
            "профиль. ID есть в карточке заказа (строка «Telegram»). Отменить нельзя.")
        return
    orders = await db.forget_user(int(arg))
    if orders is None:
        await message.answer(f"Пользователя <code>{arg}</code> в базе нет.")
        return
    log.info("Удалены данные пользователя %s по команде админа %s", arg, message.from_user.id)
    await message.answer(
        f"Данные пользователя <code>{arg}</code> удалены, заказов: {orders}.\n"
        "Сообщения о его заказах в этом чате удалите вручную.")


@router.message(Command("mailing"))
async def mailing(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    await broadcast(message, command, bot, await db.active_user_ids(), db)


@router.message(Command("test_mailing"))
async def test_mailing(message: Message, command: CommandObject, bot: Bot, settings: Settings) -> None:
    await broadcast(message, command, bot, sorted(settings.admin_ids), None)
