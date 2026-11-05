"""Команды покупателя: /start, /my_orders, /logout; блокировка бота."""

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import KICKED, MEMBER, ChatMemberUpdatedFilter, Command, CommandStart
from aiogram.types import CallbackQuery, ChatMemberUpdated, LinkPreviewOptions, Message

from app import texts
from app.bot import keyboards as kb
from app.config import Settings
from app.db import Database
from app.notify import send_many

router = Router(name="user")


@router.message(CommandStart())
async def start(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    user = message.from_user
    is_new = await db.upsert_user(user.id, user.username, user.full_name)
    if is_new:
        await send_many(bot, settings.admin_ids, texts.new_user(user.id, user.full_name, user.username))
    await db.set_bot_dm(user.id, True)  # нажал Start — теперь бот может писать о заказах

    if await db.start_confirmed(user.id):
        await message.answer(texts.start(), reply_markup=kb.webapp(settings.webapp_url),
                             link_preview_options=LinkPreviewOptions(is_disabled=True))
        return

    # ещё не нажал «Продолжить»: прошлое сообщение с документами убираем, присылаем такое же
    old_id = await db.start_message_id(user.id)
    if old_id:
        try:
            await bot.delete_message(user.id, old_id)
        except TelegramBadRequest:
            pass
    sent = await message.answer(texts.start_legal(settings.webapp_url), reply_markup=kb.start_continue(),
                                link_preview_options=LinkPreviewOptions(is_disabled=True))
    await db.set_start_message_id(user.id, sent.message_id)


@router.callback_query(F.data == kb.START_CONTINUE)
async def start_continue(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    """«Продолжить» на первом запуске: убираем документы и показываем обычный старт."""
    await call.answer()
    await db.confirm_start(call.from_user.id)
    if call.message:
        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass  # сообщение уже удалено или слишком старое — просто отправим обычный старт
    await bot.send_message(call.from_user.id, texts.start(),
                           reply_markup=kb.webapp(settings.webapp_url),
                           link_preview_options=LinkPreviewOptions(is_disabled=True))


@router.message(Command("my_orders"))
async def my_orders(message: Message, db: Database) -> None:
    await db.touch_user(message.from_user.id)
    orders = await db.user_orders(message.from_user.id)
    if not orders:
        await message.answer("Нет текущих заказов.")
        return
    for part in texts.chunks(["<b>📦 Текущие заказы</b>", *map(texts.order_for_user, orders)]):
        await message.answer(part)


@router.message(Command("logout"))
async def logout(message: Message, db: Database) -> None:
    """Выход с сайта на всех устройствах — если вход был с чужого компьютера или телефон потерян."""
    count = await db.delete_user_sessions(message.from_user.id)
    await message.answer("Готово: вы вышли с сайта на всех устройствах." if count
                         else "Вы не входили на сайт, выходить неоткуда.")


@router.my_chat_member(F.chat.type == "private", ChatMemberUpdatedFilter(KICKED))
async def bot_blocked(event: ChatMemberUpdated, db: Database) -> None:
    await db.set_bot_dm(event.from_user.id, False)


@router.my_chat_member(F.chat.type == "private", ChatMemberUpdatedFilter(MEMBER))
async def bot_unblocked(event: ChatMemberUpdated, db: Database) -> None:
    await db.set_bot_dm(event.from_user.id, True)
