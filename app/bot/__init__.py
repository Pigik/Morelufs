"""Сборка диспетчера aiogram и настройка бота (команды, кнопка меню)."""

import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeChat, MenuButtonWebApp, WebAppInfo

from app.bot import admin, user
from app.config import Settings
from app.db import Database

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Открыть магазин"),
    BotCommand(command="my_orders", description="Мои заказы"),
    BotCommand(command="logout", description="Выйти с сайта на всех устройствах"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="edit_offer", description="Заказы: статусы и трек-номера"),
    BotCommand(command="mailing", description="Рассылка всем пользователям"),
    BotCommand(command="test_mailing", description="Тестовая рассылка админам"),
    BotCommand(command="cancel", description="Отменить ввод"),
]


def create_dispatcher(settings: Settings, db: Database) -> Dispatcher:
    dp = Dispatcher(settings=settings, db=db)  # доступны в хендлерах как аргументы settings, db
    dp.include_routers(admin.router, user.router)  # admin первым: его FSM-ввод важнее
    return dp


async def setup_bot(bot: Bot, settings: Settings) -> None:
    """Команды и кнопка меню. Ошибки не фатальны: бот работает и без них."""
    try:
        await bot.set_my_commands(USER_COMMANDS)
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=settings.webapp_url))
        )
    except Exception as e:
        log.warning("Не удалось настроить команды/меню бота: %s", e)
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:  # админ ещё не писал боту
            log.warning("Команды для админа %s не установлены: %s", admin_id, e)
