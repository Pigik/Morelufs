"""Точка входа: `python -m app` — HTTP-сервер и long polling бота в одном процессе."""

import asyncio
import logging
import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiohttp import web

from app import texts
from app.api import apply_payment, create_app, make_clients
from app.bot import create_dispatcher, setup_bot
from app.catalog import Catalog
from app.config import CATALOG_PATH, EXPIRE_INTERVAL, PAYMENT_TTL_HOURS, PURGE_INTERVAL, RETENTION_YEARS, Settings
from app.db import Database
from app.notify import send_to_customer
from app.payments import PaymentError

log = logging.getLogger("app")


async def purge_forever(db: Database) -> None:
    """Раз в сутки удаляет данные покупателей, чей срок хранения истёк (политика ПДн, разд. 4)."""
    while True:
        try:
            if ids := await db.purge_inactive(RETENTION_YEARS):
                log.info("Удалены данные %s пользователей: срок хранения истёк", len(ids))
        except Exception:
            log.exception("Не удалось удалить устаревшие данные")
        await asyncio.sleep(PURGE_INTERVAL)


async def expire_unpaid(bot: Bot, db: Database, settings: Settings, payments) -> int:
    """Отменяет заказы, не оплаченные за сутки (оферта, п. 4.5). Возвращает число отменённых."""
    cancelled = 0
    for order in await db.expired_unpaid(PAYMENT_TTL_HOURS):
        if payments is not None and order.payment_id:
            # уведомление об оплате могло потеряться — перед отменой спрашиваем ЮKassa
            try:
                payment = await payments.get(order.payment_id)
            except PaymentError as e:
                log.warning("Заказ #%s: статус платежа не получен, отменим позже: %s", order.id, e)
                continue
            if payment.status == "succeeded":
                await apply_payment(bot, db, settings.admin_ids, order, payment)
                continue
        if not await db.close_unpaid(order.id, "expired"):
            continue  # оплатили, пока проверяли
        cancelled += 1
        log.info("Заказ #%s отменён: не оплачен за %s ч", order.id, PAYMENT_TTL_HOURS)
        # платёж в pending по API не отменить — ЮKassa сама отменит его по таймауту; наша ссылка «Оплатить»
        # уже не ведёт на оплату, а если заплатят по старой странице, apply_payment всё равно засчитает деньги
        order = await db.get_order(order.id)
        await send_to_customer(bot, db, order.user_id, texts.order_cancelled_unpaid(order))
    return cancelled


async def expire_forever(bot: Bot, db: Database, settings: Settings, payments) -> None:
    while True:
        try:
            await expire_unpaid(bot, db, settings, payments)
        except Exception:
            log.exception("Не удалось отменить неоплаченные заказы")
        await asyncio.sleep(EXPIRE_INTERVAL)


async def main() -> None:
    settings = Settings.from_env()
    catalog = Catalog.load(CATALOG_PATH)
    db = await Database.open(settings.db_path)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    clients = make_clients(settings)  # одни и те же клиенты у сайта и бота
    dp = create_dispatcher(settings, db, **clients)

    runner = web.AppRunner(create_app(settings, db, catalog, bot, **clients), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, settings.host, settings.port).start()
    log.info("HTTP на %s:%s, товаров в каталоге: %s", settings.host, settings.port, len(catalog.products))
    log.info("Оплата: %s, СДЭК: %s, Почта: %s", *("вкл" if c else "выкл" for c in clients.values()))
    purge = asyncio.create_task(purge_forever(db))
    expire = asyncio.create_task(expire_forever(bot, db, settings, clients["payments"]))
    try:
        await setup_bot(bot, settings)
        await dp.start_polling(bot)  # сам ловит SIGTERM/SIGINT и закрывает сессию бота
    finally:
        purge.cancel()
        expire.cancel()
        await runner.cleanup()
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)  # не логировать каждый апдейт
    asyncio.run(main())
