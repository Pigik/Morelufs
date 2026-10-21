"""Отправка сообщений, которая не роняет вызывающий код."""

import asyncio
import logging

from aiogram import Bot

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
