"""HTTP: API для Mini App и раздача статики из web/. Снаружи стоит Caddy (HTTPS)."""

import logging
import math
import random
import time

from aiogram import Bot
from aiohttp import web
from pydantic import ValidationError

from app import texts
from app.auth import verify_init_data
from app.bot import keyboards as kb
from app.catalog import FIELD_NAMES, Catalog, OrderError, OrderIn, price_cart
from app.config import DELIVERY, MAX_CART_ITEMS, PAYMENT, RATE_LIMIT_SECONDS, Settings
from app.db import Database, Order
from app.notify import send_many, send_safe

log = logging.getLogger(__name__)

SETTINGS = web.AppKey("settings", Settings)
DB = web.AppKey("db", Database)
CATALOG = web.AppKey("catalog", Catalog)
BOT = web.AppKey("bot", Bot)
LAST_ORDER = web.AppKey("last_order", dict)


def fail(description: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "description": description}, status=status)


def tg_user(request: web.Request) -> dict | None:
    return verify_init_data(request.headers.get("X-TG-Init-Data", ""), request.app[SETTINGS].bot_token)


def wait_seconds(app: web.Application, user_id: int) -> int:
    """Секунд до следующего разрешённого заказа (0 — можно). Старые записи чистятся по ходу."""
    now, last = time.monotonic(), app[LAST_ORDER]
    if len(last) > 1000:
        for uid in [u for u, t in last.items() if now - t >= RATE_LIMIT_SECONDS]:
            del last[uid]
    passed = now - last.get(user_id, -math.inf)
    return 0 if passed >= RATE_LIMIT_SECONDS else math.ceil(RATE_LIMIT_SECONDS - passed)


def order_json(o: Order) -> dict:
    return {
        "id": o.id, "status": o.status, "track": o.track, "created_at": o.created_msk.isoformat(),
        "total": o.total, "delivery": DELIVERY.get(o.delivery, o.delivery), "address": o.address,
        "payment": PAYMENT.get(o.payment, o.payment), "phone": o.phone, "telegram": o.telegram,
        "email": o.email, "comment": o.comment,
        "items": [{"title": i.title, "size": i.size, "qty": i.qty, "price": i.price} for i in o.items],
    }


async def get_catalog(request: web.Request) -> web.Response:
    catalog = request.app[CATALOG]
    return web.json_response({"products": [p.model_dump() for p in catalog.products]})


async def get_orders(request: web.Request) -> web.Response:
    user = tg_user(request)
    if user is None:
        return fail("Откройте магазин через Telegram", 401)
    orders = await request.app[DB].user_orders(user["id"])
    return web.json_response({"ok": True, "orders": [order_json(o) for o in orders]})


async def post_order(request: web.Request) -> web.Response:
    app = request.app
    user = tg_user(request)
    if user is None:
        return fail("Откройте магазин через Telegram", 401)
    uid, username = user["id"], user.get("username")

    try:
        raw = await request.json()
    except ValueError:
        return fail("Некорректный запрос")
    if not isinstance(raw, dict):
        return fail("Некорректный запрос")

    if isinstance(raw.get("cart"), list) and len(raw["cart"]) > MAX_CART_ITEMS:
        if not wait_seconds(app, uid):  # не даём заспамить админов этим же алертом
            app[LAST_ORDER][uid] = time.monotonic()
            await send_many(app[BOT], app[SETTINGS].admin_ids,
                            f"⚠️ <b>Подозрительный заказ</b>: корзина из {len(raw['cart'])} позиций "
                            f"от <code>{uid}</code> @{texts.esc(username or '-')}. Запрос отклонён.")
        return fail(f"В корзине может быть не больше {MAX_CART_ITEMS} позиций")

    try:
        data = OrderIn.model_validate(raw)
        items, total = price_cart(app[CATALOG], data.cart)
    except ValidationError as e:
        field = str(e.errors()[0]["loc"][0])
        return fail(f"Проверьте поле «{FIELD_NAMES.get(field, field)}»")
    except OrderError as e:
        return fail(str(e))

    # проверка и отметка без await между ними — двойной клик не создаст два заказа
    if wait := wait_seconds(app, uid):
        return fail(f"Слишком много запросов. Подождите {wait} сек.", 429)
    app[LAST_ORDER][uid] = time.monotonic()

    qr_list = app[CATALOG].payment_qr
    qr_index = random.randrange(len(qr_list)) + 1 if data.payment == "qr" and qr_list else None
    full_name = " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))

    db = app[DB]
    await db.upsert_user(uid, username, full_name)
    fields = {**data.model_dump(exclude={"cart"}), "qr_index": qr_index, "total": total}
    order_id = await db.create_order(uid, fields, items)
    order = await db.get_order(order_id)
    log.info("Заказ #%s от %s на %s руб.", order_id, uid, total)

    bot = app[BOT]
    await send_many(bot, app[SETTINGS].admin_ids, texts.order_for_admin(order, "📦 НОВЫЙ ЗАКАЗ"),
                    reply_markup=kb.open_order(order_id))
    await send_safe(bot, uid, texts.order_created(order))

    return web.json_response({"ok": True, "order": {
        "id": order_id, "total": total, "qr": qr_list[qr_index - 1] if qr_index else None,
    }})


@web.middleware
async def errors_and_cache(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("Ошибка при обработке %s %s", request.method, request.path)
        return fail("Внутренняя ошибка сервера", 500)
    if request.path.startswith("/assets/"):
        response.headers.setdefault("Cache-Control", "public, max-age=86400")
    else:  # html, css, js, api — всегда свежие (браузер перепроверяет по Last-Modified)
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


def create_app(settings: Settings, db: Database, catalog: Catalog, bot: Bot) -> web.Application:
    app = web.Application(middlewares=[errors_and_cache], client_max_size=64 * 1024)
    app[SETTINGS], app[DB], app[CATALOG], app[BOT], app[LAST_ORDER] = settings, db, catalog, bot, {}

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(settings.web_dir / "index.html")

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/catalog", get_catalog)
    app.router.add_get("/api/orders", get_orders)
    app.router.add_post("/api/order", post_order)
    for folder in ("assets", "css", "js"):
        app.router.add_static(f"/{folder}", settings.web_dir / folder)
    return app
