"""Отправка сообщений, которая не роняет вызывающий код."""

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.db import Database

log = logging.getLogger(__name__)


async def send_safe(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except Exception as e:
        log.warning("Сообщение для %s не отправлено: %s", chat_id, e)
        return False


async def send_many(bot: Bot, chat_ids, text: str, **kwargs) -> None:
    await asyncio.gather(*(send_safe(bot, cid, text, **kwargs) for cid in chat_ids))


def cannot_write(error: Exception) -> bool:
    """Telegram не даёт боту писать: покупатель не нажимал Start, заблокировал бота или удалил аккаунт."""
    return isinstance(error, TelegramForbiddenError) or (
        isinstance(error, TelegramBadRequest) and "chat not found" in str(error))


async def send_to_customer(bot: Bot, db: Database, user_id: int, text: str, **kwargs) -> bool:
    """Сообщение покупателю; заодно запоминаем, может ли бот ему писать (для «!» в корзине на сайте)."""
    try:
        await bot.send_message(user_id, text, **kwargs)
    except Exception as e:
        if cannot_write(e):
            await db.set_bot_dm(user_id, False)
        log.warning("Сообщение для %s не отправлено: %s", user_id, e)
        return False
    await db.set_bot_dm(user_id, True)
    return True


async def can_write(bot: Bot, user_id: int) -> bool | None:
    """Проверка без сообщения: «печатает…» на пару секунд. None — Telegram не ответил, ничего не знаем."""
    try:
        await bot.send_chat_action(user_id, "typing")
        return True
    except Exception as e:
        return False if cannot_write(e) else None
