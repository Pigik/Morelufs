"""HTTP: API для Mini App и раздача статики из web/. Снаружи стоит Caddy (HTTPS)."""

import hashlib
import logging
import math
import random
import secrets
import time
from urllib.parse import urlencode, urlparse

from aiogram import Bot
from aiohttp import web
from pydantic import ValidationError

from app import texts
from app.auth import verify_init_data
from app.bot import keyboards as kb
from app.catalog import FIELD_NAMES, Catalog, OrderError, OrderIn, price_cart
from app.config import DELIVERY, LOGIN_TTL, MAX_CART_ITEMS, PAYMENT, RATE_LIMIT_SECONDS, SESSION_DAYS, Settings
from app.db import Database, Order
from app.notify import send_many, send_safe
from app.telegram_login import LoginError, TelegramLogin, new_pkce, phone_from_claims

log = logging.getLogger(__name__)

SETTINGS = web.AppKey("settings", Settings)
DB = web.AppKey("db", Database)
CATALOG = web.AppKey("catalog", Catalog)
BOT = web.AppKey("bot", Bot)
LAST_ORDER = web.AppKey("last_order", dict)
LOGIN = web.AppKey("login", object)  # TelegramLogin или None, если вход на сайте не настроен

SESSION_COOKIE = "mls_session"
STATE_COOKIE = "mls_login"  # привязывает возврат из Telegram к браузеру, который начал вход
NEXT_SCREENS = ("orders", "checkout")  # куда можно вернуть после входа


def fail(description: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "description": description}, status=status)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def current_user(request: web.Request) -> dict | None:
    """Покупатель запроса: из подписи мини-аппа (initData) или из cookie входа на сайте.

    Возвращает {id, username, full_name, phone, via}; via — "miniapp" или "site".
    """
    app = request.app
    init_data = request.headers.get("X-TG-Init-Data")
    if init_data:
        user = verify_init_data(init_data, app[SETTINGS].bot_token)
        if user is None:
            return None
        full_name = " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))
        return {"id": user["id"], "username": user.get("username"), "full_name": full_name,
                "phone": None, "via": "miniapp"}
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        user = await app[DB].session_user(token_hash(token), int(time.time()))
        if user:
            return {**user, "via": "site"}
    return None


def same_origin(request: web.Request) -> bool:
    """Защита от CSRF для запросов с cookie: POST с чужого сайта отклоняем."""
    origin = request.headers.get("Origin")
    return origin is None or urlparse(origin).netloc == request.host


def set_cookie(response: web.StreamResponse, settings: Settings, name: str, value: str, max_age: int,
               path: str = "/") -> None:
    response.set_cookie(name, value, max_age=max_age, path=path, httponly=True, samesite="Lax",
                        secure=settings.webapp_url.startswith("https://"))


def redirect(location: str) -> web.Response:
    return web.Response(status=302, headers={"Location": location})


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
    user = await current_user(request)
    if user is None:
        return fail("Войдите через Telegram, чтобы увидеть заказы", 401)
    orders = await request.app[DB].user_orders(user["id"])
    return web.json_response({"ok": True, "orders": [order_json(o) for o in orders]})


async def post_order(request: web.Request) -> web.Response:
    app = request.app
    user = await current_user(request)
    if user is None:
        return fail("Войдите через Telegram, чтобы оформить заказ", 401)
    if user["via"] == "site" and not same_origin(request):
        return fail("Запрос отклонён", 403)
    uid, username = user["id"], user["username"]

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
    db = app[DB]
    await db.upsert_user(uid, username, user["full_name"])
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


# --- вход на сайт через Telegram ---

async def get_me(request: web.Request) -> web.Response:
    """Кто открыл сайт. Фронт по нему решает, показывать ли вход."""
    user = await current_user(request)
    if user and not user["phone"]:  # в мини-аппе номер известен, если человек хоть раз входил на сайт
        stored = await request.app[DB].get_user(user["id"])
        user["phone"] = stored and stored["phone"]
    return web.json_response({
        "ok": True,
        "login_enabled": request.app[LOGIN] is not None,
        "user": user and {k: user[k] for k in ("id", "username", "full_name", "phone", "via")},
    })


async def auth_login(request: web.Request) -> web.Response:
    """Начало входа: запоминаем state/PKCE и отправляем покупателя на oauth.telegram.org."""
    app = request.app
    login: TelegramLogin | None = app[LOGIN]
    if login is None:
        return redirect("/?" + urlencode({"login": "disabled"}))
    next_screen = request.query.get("next", "")
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
    verifier, challenge = new_pkce()
    await app[DB].add_login_attempt(state, verifier, nonce, next_screen if next_screen in NEXT_SCREENS else "",
                                    int(time.time()), LOGIN_TTL)
    response = redirect(login.authorize_url(state, challenge, nonce))
    set_cookie(response, app[SETTINGS], STATE_COOKIE, state, LOGIN_TTL, path="/auth")
    return response


async def auth_callback(request: web.Request) -> web.Response:
    """Возврат из Telegram: проверяем state, меняем code на id_token, создаём сессию."""
    app, query = request.app, request.query
    settings, db = app[SETTINGS], app[DB]
    now = int(time.time())
    state = query.get("state", "")
    attempt = await db.pop_login_attempt(state, now, LOGIN_TTL) if state else None

    def back(result: str) -> web.Response:
        params = {"login": result}
        if attempt and attempt["next"]:
            params["next"] = attempt["next"]
        response = redirect("/?" + urlencode(params))
        response.del_cookie(STATE_COOKIE, path="/auth")
        return response

    if query.get("error"):  # покупатель нажал «Отмена» в Telegram
        log.info("Вход отменён: %s", query.get("error"))
        return back("cancelled")
    # state должен прийти и в адресе, и в cookie этого же браузера — иначе это чужая ссылка входа
    same_browser = secrets.compare_digest(request.cookies.get(STATE_COOKIE, "").encode(), state.encode())
    if attempt is None or not same_browser or not query.get("code"):
        return back("expired")
    login: TelegramLogin | None = app[LOGIN]
    if login is None:
        return back("disabled")
    try:
        claims = await login.exchange(query["code"], attempt["verifier"], attempt["nonce"])
    except LoginError as e:
        log.warning("Вход через Telegram не удался: %s", e)
        return back("failed")

    uid = claims["id"]
    username = claims.get("preferred_username") or None
    full_name = claims.get("name") or " ".join(filter(None, (claims.get("given_name"), claims.get("family_name"))))
    if await db.upsert_user(uid, username, full_name):
        await send_many(app[BOT], settings.admin_ids, texts.new_user(uid, full_name, username) + " (сайт)")
    if phone := phone_from_claims(claims):
        await db.set_phone(uid, phone)

    token = secrets.token_urlsafe(32)
    await db.create_session(token_hash(token), uid, now + SESSION_DAYS * 86400, now)
    log.info("Вход на сайт: %s", uid)
    response = back("ok")
    set_cookie(response, settings, SESSION_COOKIE, token, SESSION_DAYS * 86400)
    return response


async def post_logout(request: web.Request) -> web.Response:
    if not same_origin(request):
        return fail("Запрос отклонён", 403)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await request.app[DB].delete_session(token_hash(token))
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


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
    elif request.path.startswith(("/auth/", "/api/me")):  # ответы зависят от cookie — не кэшировать
        response.headers.setdefault("Cache-Control", "no-store")
    else:  # html, css, js, api — всегда свежие (браузер перепроверяет по Last-Modified)
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


def create_app(settings: Settings, db: Database, catalog: Catalog, bot: Bot, login=None) -> web.Application:
    """login — подмена клиента Telegram Login в тестах; по умолчанию создаётся из настроек."""
    app = web.Application(middlewares=[errors_and_cache], client_max_size=64 * 1024)
    app[SETTINGS], app[DB], app[CATALOG], app[BOT], app[LAST_ORDER] = settings, db, catalog, bot, {}
    if login is None and settings.login_enabled:
        login = TelegramLogin(settings.login_client_id, settings.login_client_secret, settings.login_redirect_uri)
    app[LOGIN] = login

    async def close_login(_: web.Application) -> None:
        if isinstance(login, TelegramLogin):
            await login.close()

    app.on_cleanup.append(close_login)

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(settings.web_dir / "index.html")

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/catalog", get_catalog)
    app.router.add_get("/api/orders", get_orders)
    app.router.add_post("/api/order", post_order)
    app.router.add_get("/api/me", get_me)
    app.router.add_post("/api/logout", post_logout)
    app.router.add_get("/auth/login", auth_login)
    app.router.add_get("/auth/callback", auth_callback)
    for folder in ("assets", "css", "js"):
        app.router.add_static(f"/{folder}", settings.web_dir / folder)
    return app
