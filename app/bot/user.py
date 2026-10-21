"""Команды покупателя: /start, /my_orders."""

from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app import texts
from app.bot import keyboards as kb
from app.config import Settings
from app.db import Database
from app.notify import send_many

router = Router(name="user")


@router.message(CommandStart())
async def start(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    user = message.from_user
    if await db.upsert_user(user.id, user.username, user.full_name):
        await send_many(bot, settings.admin_ids, texts.new_user(user.id, user.full_name, user.username))
    await message.answer("Привет! Вот наше приложение:", reply_markup=kb.webapp(settings.webapp_url))


@router.message(Command("my_orders"))
async def my_orders(message: Message, db: Database) -> None:
    orders = await db.user_orders(message.from_user.id)
    if not orders:
        await message.answer("У вас пока нет заказов.")
        return
    for part in texts.chunks(["<b>📦 Ваши заказы</b>", *map(texts.order_for_user, orders)]):
        await message.answer(part)
