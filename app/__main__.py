"""Точка входа: `python -m app` — HTTP-сервер и long polling бота в одном процессе."""

import asyncio
import logging
import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiohttp import web

from app.api import create_app
from app.bot import create_dispatcher, setup_bot
from app.catalog import Catalog
from app.config import CATALOG_PATH, Settings
from app.db import Database

log = logging.getLogger("app")


async def main() -> None:
    settings = Settings.from_env()
    catalog = Catalog.load(CATALOG_PATH)
    db = await Database.open(settings.db_path)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = create_dispatcher(settings, db)

    runner = web.AppRunner(create_app(settings, db, catalog, bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, settings.host, settings.port).start()
    log.info("HTTP на %s:%s, товаров в каталоге: %s", settings.host, settings.port, len(catalog.products))
    try:
        await setup_bot(bot, settings)
        await dp.start_polling(bot)  # сам ловит SIGTERM/SIGINT и закрывает сессию бота
    finally:
        await runner.cleanup()
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)  # не логировать каждый апдейт
    asyncio.run(main())
