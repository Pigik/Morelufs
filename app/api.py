"""HTTP: API для Mini App и раздача статики из web/. Снаружи стоит Caddy (HTTPS)."""

import hashlib
import hmac
import logging
import math
import re
import secrets
import time
from dataclasses import asdict
from urllib.parse import urlencode, urlparse

from aiogram import Bot
from aiohttp import web
from pydantic import ValidationError

from app import legal, texts
from app.auth import verify_init_data
from app.bot import keyboards as kb
from app.catalog import FIELD_NAMES, Catalog, DeliveryIn, OrderError, OrderIn, make_parcel, phone_e164, price_cart
from app.config import (
    DELIVERY,
    LOGIN_TTL,
    MAX_CART_ITEMS,
    PAYMENT,
    RATE_LIMIT_SECONDS,
    SESSION_DAYS,
    Settings,
)
from app.db import Database, Order
from app.delivery import DeliveryError
from app.delivery.cdek import CdekClient
from app.delivery.pochta import PochtaClient
from app.notify import can_write, send_many, send_to_customer
from app.payments import Payment, PaymentError, ReceiptLine, YooKassa
from app.telegram_login import LoginError, TelegramLogin, new_pkce, phone_from_claims

log = logging.getLogger(__name__)

SETTINGS = web.AppKey("settings", Settings)
DB = web.AppKey("db", Database)
CATALOG = web.AppKey("catalog", Catalog)
BOT = web.AppKey("bot", Bot)
LAST_ORDER = web.AppKey("last_order", dict)
LOGIN = web.AppKey("login", object)  # TelegramLogin или None, если вход на сайте не настроен
LEGAL = web.AppKey("legal", dict)  # slug → legal.LegalPage: оферта, политика, согласие, реквизиты
# клиенты внешних сервисов; None — сервис не настроен (нет ключей в .env)
PAYMENTS = web.AppKey("payments", object)
CDEK = web.AppKey("cdek", object)
POCHTA = web.AppKey("pochta", object)

SESSION_COOKIE = "mls_session"
STATE_COOKIE = "mls_login"  # привязывает возврат из Telegram к браузеру, который начал вход
NEXT_SCREENS = ("orders", "checkout")  # куда можно вернуть после входа
CHECKOUT_OFF = "Оформление заказов временно недоступно. Загляните чуть позже или напишите нам в Telegram."
PAYMENT_FAILED = "Не удалось создать платёж. Попробуйте ещё раз через минуту"
PAYMENT_DOWN = "Платёжный сервис временно недоступен. Попробуйте ещё раз через минуту."
YOOKASSA_ID = re.compile(r"[\w-]{1,64}")  # id платежа/возврата из уведомления подставляется в адрес запроса


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


def pay_link(settings: Settings, order_id: int) -> str:
    """Наша постоянная ссылка «Оплатить»: ведёт на действующую страницу ЮKassa или создаёт новый платёж,
    если прежний отменён (ЮKassa отменяет платёж после отказа банка или по таймауту, а заказ ждёт сутки).
    Подпись — чтобы по чужим номерам заказов нельзя было плодить платежи."""
    sig = hmac.new(b"pay:" + settings.bot_token.encode(), str(order_id).encode(), hashlib.sha256).hexdigest()[:24]
    return f"{settings.webapp_url.rstrip('/')}/pay/{order_id}/{sig}"


def pay_url(settings: Settings, o: Order) -> str | None:
    return pay_link(settings, o.id) if o.live_payment_url else None


def order_json(o: Order, settings: Settings) -> dict:
    return {
        "id": o.id, "status": o.status, "track": o.track, "created_at": o.created_msk.isoformat(),
        "total": o.total, "goods_total": o.goods_total, "delivery_cost": o.delivery_cost,
        "delivery": DELIVERY.get(o.delivery, o.delivery), "address": o.address,
        "payment": PAYMENT.get(o.payment, o.payment), "payment_status": o.payment_status,
        "payment_url": pay_url(settings, o), "phone": o.phone, "telegram": o.telegram,
        "email": o.email, "comment": o.comment,
        "items": [{"title": i.title, "size": i.size, "qty": i.qty, "price": i.price} for i in o.items],
    }


def make_clients(settings: Settings, payments=None, cdek=None, pochta=None) -> dict:
    """Клиенты ЮKassa, СДЭК и Почты: переданные — как есть, остальные из настроек; без ключей — None."""
    if payments is None and settings.payments_enabled:
        payments = YooKassa(settings.yookassa_shop_id, settings.yookassa_secret_key,
                            vat_code=settings.yookassa_vat_code)
    if cdek is None and settings.cdek_enabled:
        cdek = CdekClient(settings.cdek_client_id, settings.cdek_client_secret, base_url=settings.cdek_api_url,
                          from_city_code=settings.cdek_from_city_code, tariff_code=settings.cdek_tariff,
                          shipment_point=settings.cdek_shipment_point)
    if pochta is None and settings.pochta_enabled:
        pochta = PochtaClient(settings.pochta_token, settings.pochta_login, settings.pochta_password,
                              from_index=settings.pochta_from_index)
    return {"payments": payments, "cdek": cdek, "pochta": pochta}


def checkout_state(app: web.Application) -> dict:
    delivery = {"cdek": app[CDEK] is not None, "pochta": app[POCHTA] is not None}
    enabled = app[PAYMENTS] is not None and any(delivery.values())
    return {"enabled": enabled, "delivery": delivery, "message": None if enabled else CHECKOUT_OFF}


_alerted: set[str] = set()  # платежи, о которых админ уже предупреждён (опрос статуса идёт раз в 3 с)


async def alert_once(bot: Bot, admin_ids, payment_id: str, text: str) -> None:
    if payment_id not in _alerted:
        _alerted.add(payment_id)
        await send_many(bot, admin_ids, text)


async def apply_payment(bot: Bot, db: Database, admin_ids, order: Order, payment: Payment) -> Order:
    """Платёж, полученный от ЮKassa по API (не из тела уведомления), → заказ. Повтор ничего не делает."""
    if payment.status == "succeeded":
        if payment.amount != order.total * 100:
            log.error("Заказ #%s: оплачено %s коп., а заказ на %s руб.", order.id, payment.amount, order.total)
            # заказ не отмечаем оплаченным, но деньги пришли — пусть админ разберётся вручную
            await alert_once(bot, admin_ids, payment.id,
                             f"⚠️ <b>Заказ #{order.id}</b>: ЮKassa подтвердила оплату на сумму, не равную заказу "
                             f"({texts.esc(payment.amount)} коп. вместо {order.total * 100}). Проверьте платёж "
                             f"<code>{texts.esc(payment.id)}</code> в личном кабинете ЮKassa.")
            return order
        if order.payment_id != payment.id:
            if order.payment_status in ("paid", "refunded"):
                log.error("Заказ #%s: второй успешный платёж %s", order.id, payment.id)
                await alert_once(bot, admin_ids, payment.id,
                                 f"⚠️ <b>Заказ #{order.id}</b> оплачен второй раз (платёж "
                                 f"<code>{texts.esc(payment.id)}</code>). Верните его в личном кабинете ЮKassa.")
                return order
            # заплатили по старой ссылке, пока создавался новый платёж: возвращать деньги — по этому
            order = await db.set_payment(order.id, payment.id, order.payment_url or "") or order
        if await db.mark_paid(order.id):
            order = await db.get_order(order.id)
            log.info("Заказ #%s оплачен (%s)", order.id, order.payment_id)
            await send_many(bot, admin_ids, texts.order_for_admin(order, "💰 ОПЛАЧЕН ЗАКАЗ"),
                            reply_markup=kb.open_order(order.id))
            await send_to_customer(bot, db, order.user_id, texts.order_paid(order))
    elif payment.status == "canceled":
        # заказ не отменяем: сутки на оплату по оферте, ссылка «Оплатить» создаст новый платёж
        log.info("Заказ #%s: платёж %s отменён (%s)", order.id, payment.id, payment.cancel_reason)
    elif payment.status == "waiting_for_capture":
        log.warning("Заказ #%s: платёж %s ждёт подтверждения, хотя создан с capture=true", order.id, payment.id)
    return order


async def apply_refund(bot: Bot, db: Database, order: Order) -> Order:
    if await db.mark_refunded(order.id):
        order = await db.get_order(order.id)
        log.info("Заказ #%s: деньги возвращены", order.id)
        await send_to_customer(bot, db, order.user_id, texts.refunded(order))
    return order


async def create_payment(app: web.Application, order: Order, idempotence_key: str | None = None) -> Payment:
    settings = app[SETTINGS]
    receipt = None
    if settings.yookassa_vat_code is not None:
        receipt = [ReceiptLine(f"{i.title} ({i.size})", i.price, i.qty) for i in order.items]
        if order.delivery_cost:
            receipt.append(ReceiptLine(f"Доставка {DELIVERY.get(order.delivery, order.delivery)}",
                                       order.delivery_cost, 1, "service"))
    return await app[PAYMENTS].create(
        order_id=order.id, amount=order.total, description=f"Заказ #{order.id} в MORELUFS",
        return_url=pay_link(settings, order.id) + "?return=1", email=order.email, phone=phone_e164(order.phone),
        receipt=receipt, idempotence_key=idempotence_key)


async def quote_delivery(app: web.Application, data: DeliveryIn, items: list[dict], goods_total: int) -> dict:
    """Стоимость и адрес доставки считает сервер. OrderError/DeliveryError — текст для покупателя."""
    parcel = make_parcel(app[CATALOG], items)
    info = {"parcel": asdict(parcel),
            "item_weights": {str(i["product_id"]): app[CATALOG].get(i["product_id"]).weight for i in items}}
    if data.delivery == "cdek":
        cdek: CdekClient | None = app[CDEK]
        if cdek is None:
            raise OrderError("Доставка СДЭК сейчас недоступна. Выберите другой способ.")
        point = await cdek.point(data.cdek_city_code, data.cdek_point)
        if point is None:
            raise OrderError("Пункт выдачи СДЭК не найден. Выберите его заново.")
        quote = await cdek.quote(data.cdek_city_code, parcel)
        address = point.address
        info.update(cdek_city_code=data.cdek_city_code, cdek_point=point.code, point_name=point.name)
    else:
        pochta: PochtaClient | None = app[POCHTA]
        if pochta is None:
            raise OrderError("Доставка Почтой России сейчас недоступна. Выберите другой способ.")
        post = await pochta.normalize(data.address)
        # без объявленной ценности: так дешевле для покупателя (решение продавца)
        quote = await pochta.quote(post.index, parcel)
        address = post.full or ", ".join(filter(None, (post.index, post.region, post.place, post.street,
                                                       post.house, post.room)))
        info.update(post_address=asdict(post), declared_value=0)
    info.update(days_min=quote.days_min, days_max=quote.days_max)
    return {"cost": quote.cost, "address": address, "info": info}


def validation_error(e: ValidationError) -> web.Response:
    err = e.errors()[0]
    if not err["loc"]:  # проверка доставки целиком (DeliveryIn.delivery_target) — текст уже для покупателя
        return fail(err["msg"])
    field = str(err["loc"][0])
    return fail(f"Проверьте поле «{FIELD_NAMES.get(field, field)}»")


async def read_json(request: web.Request) -> dict | None:
    try:
        raw = await request.json()
    except ValueError:
        return None
    return raw if isinstance(raw, dict) else None


async def get_catalog(request: web.Request) -> web.Response:
    catalog = request.app[CATALOG]
    return web.json_response({"products": [p.model_dump() for p in catalog.products]})


async def get_orders(request: web.Request) -> web.Response:
    user = await current_user(request)
    if user is None:
        return fail("Войдите через Telegram, чтобы увидеть заказы", 401)
    await request.app[DB].touch_user(user["id"])
    orders = await request.app[DB].user_orders(user["id"])
    return web.json_response({"ok": True, "orders": [order_json(o, request.app[SETTINGS]) for o in orders]})


async def get_checkout(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, **checkout_state(request.app)})


async def get_cdek_cities(request: web.Request) -> web.Response:
    cdek: CdekClient | None = request.app[CDEK]
    if cdek is None:
        return fail("Доставка СДЭК сейчас недоступна", 503)
    query = request.query.get("q", "").strip()
    if len(query) > 64:
        return fail("Слишком длинный запрос")
    if len(query) < 2:
        return web.json_response({"ok": True, "cities": []})
    try:
        cities = await cdek.cities(query)
    except DeliveryError as e:
        return fail(str(e), 502)
    return web.json_response({"ok": True, "cities": [asdict(c) for c in cities]})


async def get_cdek_points(request: web.Request) -> web.Response:
    cdek: CdekClient | None = request.app[CDEK]
    if cdek is None:
        return fail("Доставка СДЭК сейчас недоступна", 503)
    city = request.query.get("city", "")
    if not city.isdigit() or not 0 < int(city) < 10**9:
        return fail("Не выбран город")
    try:
        points = await cdek.points(int(city))
    except DeliveryError as e:
        return fail(str(e), 502)
    return web.json_response({"ok": True, "points": [asdict(p) for p in points]})


async def post_quote(request: web.Request) -> web.Response:
    """Предварительный расчёт доставки для формы; доступен и без входа — ничего не сохраняет."""
    app = request.app
    raw = await read_json(request)
    if raw is None:
        return fail("Некорректный запрос")
    if isinstance(raw.get("cart"), list) and len(raw["cart"]) > MAX_CART_ITEMS:
        return fail(f"В корзине может быть не больше {MAX_CART_ITEMS} позиций")
    try:
        data = DeliveryIn.model_validate(raw)
        items, goods_total = price_cart(app[CATALOG], data.cart)
        quote = await quote_delivery(app, data, items, goods_total)
    except ValidationError as e:
        return validation_error(e)
    except (OrderError, DeliveryError) as e:
        return fail(str(e))
    info = quote["info"]
    return web.json_response({
        "ok": True, "delivery_cost": quote["cost"], "days_min": info["days_min"], "days_max": info["days_max"],
        "goods_total": goods_total, "total": goods_total + quote["cost"], "address": quote["address"],
    })


async def post_order(request: web.Request) -> web.Response:
    app = request.app
    user = await current_user(request)
    if user is None:
        return fail("Войдите через Telegram, чтобы оформить заказ", 401)
    if user["via"] == "site" and not same_origin(request):
        return fail("Запрос отклонён", 403)
    if not checkout_state(app)["enabled"]:
        return fail(CHECKOUT_OFF, 503)
    uid, username = user["id"], user["username"]

    raw = await read_json(request)
    if raw is None:
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
        items, goods_total = price_cart(app[CATALOG], data.cart)
    except ValidationError as e:
        return validation_error(e)
    except OrderError as e:
        return fail(str(e))

    # проверка и отметка без await между ними — двойной клик не создаст два заказа
    if wait := wait_seconds(app, uid):
        return fail(f"Слишком много запросов. Подождите {wait} сек.", 429)
    app[LAST_ORDER][uid] = time.monotonic()

    try:
        quote = await quote_delivery(app, data, items, goods_total)
    except (OrderError, DeliveryError) as e:
        app[LAST_ORDER].pop(uid, None)  # заказ не создан — исправить адрес и повторить можно сразу
        return fail(str(e))
    cost, total = quote["cost"], goods_total + quote["cost"]

    settings, db, bot = app[SETTINGS], app[DB], app[BOT]
    await db.upsert_user(uid, username, user["full_name"])
    fields = {**data.model_dump(exclude={"cart", "consent", "offer", "cdek_city_code", "cdek_point"}),
              "address": quote["address"], "payment": "yookassa", "payment_status": "pending", "total": total,
              "delivery_cost": cost, "delivery_data": quote["info"]}
    order_id = await db.create_order(uid, fields, items, legal.accepted_version(app[LEGAL]))

    try:
        payment = await create_payment(app, await db.get_order(order_id))
    except Exception as e:
        (log.warning if isinstance(e, PaymentError) else log.exception)("Платёж для заказа #%s не создан: %s",
                                                                         order_id, e)
        await db.delete_order(order_id)
        app[LAST_ORDER].pop(uid, None)
        return fail(PAYMENT_FAILED, 502)
    order = await db.set_payment(order_id, payment.id, payment.url)
    log.info("Заказ #%s от %s на %s руб. (доставка %s), платёж %s", order_id, uid, total, cost, payment.id)

    # админам — только после оплаты (apply_payment_status), неоплаченные заказы их не касаются
    notified = await send_to_customer(bot, db, uid, texts.order_created(order),
                                     reply_markup=kb.pay(pay_link(settings, order_id)))

    return web.json_response({"ok": True, "order": {
        "id": order_id, "total": total, "delivery_cost": cost, "payment_url": payment.url,
        "notified": notified,  # False — бот не смог написать, сайт покажет «!» с подсказкой нажать Start
    }})


async def get_order_payment(request: web.Request) -> web.Response:
    """Статус оплаты для экрана «Ожидаем оплату»; пока pending — сверяемся с ЮKassa, не дожидаясь уведомления."""
    app = request.app
    user = await current_user(request)
    if user is None:
        return fail("Войдите через Telegram", 401)
    order = await app[DB].get_order(int(request.match_info["id"]))
    if order is None or order.user_id != user["id"]:  # чужой заказ неотличим от несуществующего
        return fail("Заказ не найден", 404)
    payments: YooKassa | None = app[PAYMENTS]
    if order.payment_status == "pending" and order.payment_id and payments is not None:
        try:
            payment = await payments.get(order.payment_id)
        except PaymentError as e:
            log.warning("Статус платежа для заказа #%s: %s", order.id, e)
        else:
            order = await apply_payment(app[BOT], app[DB], app[SETTINGS].admin_ids, order, payment)
    return web.json_response({"ok": True, "id": order.id, "payment_status": order.payment_status,
                              "status": order.status, "total": order.total,
                              "payment_url": pay_url(app[SETTINGS], order)})


async def pay_redirect(request: web.Request) -> web.Response:
    """/pay/{id}/{sig} — кнопка «Оплатить» (сайт, бот) и return_url после страницы ЮKassa (?return=1)."""
    app = request.app
    settings, db = app[SETTINGS], app[DB]
    order_id = int(request.match_info["id"])
    if not hmac.compare_digest(pay_link(settings, order_id), f"{settings.webapp_url.rstrip('/')}{request.path}"):
        raise web.HTTPNotFound()
    order = await db.get_order(order_id)
    if order is None:
        raise web.HTTPNotFound()
    payments: YooKassa | None = app[PAYMENTS]
    if payments is not None and order.payment_status == "pending" and order.payment_id:
        try:
            payment = await payments.get(order.payment_id)
            order = await apply_payment(app[BOT], db, settings.admin_ids, order, payment)
            if "return" not in request.query and order.live_payment_url:
                if payment.status == "pending" and payment.url:
                    return redirect(payment.url)
                if payment.status == "canceled":
                    # ключ от отменённого платежа: два одновременных клика получат один и тот же новый платёж
                    payment = await create_payment(app, order, f"{order.id}-after-{payment.id}")
                    await db.set_payment(order.id, payment.id, payment.url)
                    log.info("Заказ #%s: новый платёж %s вместо отменённого", order.id, payment.id)
                    return redirect(payment.url)
        except PaymentError as e:
            log.warning("Оплата заказа #%s: %s", order.id, e)
            return web.Response(status=502, text=PAYMENT_DOWN)
    # оплачен, отменён или вернулись со страницы ЮKassa: экран оплаты на сайте сам покажет статус
    result = "success" if order.payment_status == "paid" else "fail"
    return redirect(f"{settings.webapp_url.rstrip('/')}/?payment={result}&order={order.id}")


async def payment_notify(request: web.Request) -> web.Response:
    """Уведомление ЮKassa. Подписи у него нет, поэтому телу не верим: платёж/возврат запрашиваем по API."""
    app = request.app
    payments: YooKassa | None = app[PAYMENTS]
    if payments is None:
        raise web.HTTPNotFound()
    data = await read_json(request)
    obj = data.get("object") if data else None
    event, object_id = (data or {}).get("event"), obj.get("id") if isinstance(obj, dict) else None
    if not isinstance(event, str) or not isinstance(object_id, str) or not YOOKASSA_ID.fullmatch(object_id):
        log.warning("Непонятное уведомление ЮKassa от %s", request.remote)
        return web.Response(status=400, text="Bad request")
    bot, db, admin_ids = app[BOT], app[DB], app[SETTINGS].admin_ids
    try:
        if event.startswith("payment."):
            payment = await payments.get(object_id)
            order = await db.order_by_payment(payment.id)
            if order is None and payment.order_id is not None:  # более старый платёж того же заказа
                order = await db.get_order(payment.order_id)
            if order is None:
                log.warning("Уведомление ЮKassa: заказ для платежа %s (%s) не найден", payment.id, payment.status)
            elif order.payment_id == payment.id or payment.status == "succeeded":
                await apply_payment(bot, db, admin_ids, order, payment)
        elif event == "refund.succeeded":
            refund = await payments.get_refund(object_id)
            order = await db.order_by_payment(refund.payment_id)
            if order is not None and refund.status == "succeeded":
                await apply_refund(bot, db, order)
    except PaymentError as e:
        log.warning("Уведомление ЮKassa %s %s: %s", event, object_id, e)
        return web.Response(status=502, text="Retry later")  # ЮKassa повторит уведомление
    return web.Response(text="OK")  # иначе ЮKassa будет повторять уведомление сутки


# --- вход на сайт через Telegram ---

async def bot_username(app: web.Application) -> str | None:
    try:
        return (await app[BOT].me()).username  # aiogram кэширует ответ getMe
    except Exception as e:
        log.warning("Не удалось узнать username бота: %s", e)
        return None


async def get_me(request: web.Request) -> web.Response:
    """Кто открыл сайт. Фронт по нему решает, показывать ли вход, ник и «!» в корзине."""
    app = request.app
    user = await current_user(request)
    public = None
    if user:
        db = app[DB]
        if not user["phone"]:  # в мини-аппе номер известен, если человек хоть раз входил на сайт
            stored = await db.get_user(user["id"])
            user["phone"] = stored and stored["phone"]
        await db.touch_user(user["id"])
        public = {k: user[k] for k in ("id", "username", "full_name", "phone", "via")}
        public["needs_bot_start"] = await db.needs_bot_start(user["id"])
    return web.json_response({
        "ok": True,
        "login_enabled": app[LOGIN] is not None,
        "bot_username": await bot_username(app),
        "user": public,
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
    # Раньше бот не мог написать, а при входе человек мог разрешить ему сообщения — проверяем
    stored = await db.get_user(uid)
    if stored and stored["bot_dm"] == 0 and await can_write(app[BOT], uid):
        await db.set_bot_dm(uid, True)

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
        # медиа заменяют под теми же именами (фото товаров, видео) — браузер должен перепроверять по Last-Modified,
        # иначе сутки показывает старый файл
        response.headers.setdefault("Cache-Control", "public, no-cache")
    elif request.path.startswith(("/auth/", "/pay/", "/api/me", "/api/payment/", "/api/order/")):
        response.headers.setdefault("Cache-Control", "no-store")  # зависят от cookie/оплаты
    else:  # html, css, js, api — всегда свежие (браузер перепроверяет по Last-Modified)
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


def create_app(settings: Settings, db: Database, catalog: Catalog, bot: Bot, login=None, payments=None, cdek=None,
               pochta=None) -> web.Application:
    """login, payments, cdek, pochta — общие с ботом клиенты (или подмены в тестах); по умолчанию — из настроек."""
    app = web.Application(middlewares=[errors_and_cache], client_max_size=64 * 1024)
    app[SETTINGS], app[DB], app[CATALOG], app[BOT], app[LAST_ORDER] = settings, db, catalog, bot, {}
    catalog.attach_variants(settings.web_dir)
    if login is None and settings.login_enabled:
        login = TelegramLogin(settings.login_client_id, settings.login_client_secret, settings.login_redirect_uri)
    app[LOGIN] = login
    clients = make_clients(settings, payments, cdek, pochta)
    app[PAYMENTS], app[CDEK], app[POCHTA] = clients["payments"], clients["cdek"], clients["pochta"]
    app[LEGAL] = legal.load(settings.web_dir / "legal")

    async def close_clients(_: web.Application) -> None:
        if isinstance(login, TelegramLogin):
            await login.close()
        for client in clients.values():
            if client is not None:
                try:
                    await client.close()
                except Exception as e:
                    log.warning("Клиент %s не закрыт: %s", type(client).__name__, e)

    app.on_cleanup.append(close_clients)

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(settings.web_dir / "index.html")

    async def favicon(_: web.Request) -> web.FileResponse:
        return web.FileResponse(settings.web_dir / "favicon.ico")

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def legal_page(request: web.Request) -> web.Response:
        page = app[LEGAL].get(request.match_info["slug"])
        if page is None:
            raise web.HTTPNotFound()
        return web.Response(text=page.html, content_type="text/html")

    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/api/catalog", get_catalog)
    app.router.add_get("/api/orders", get_orders)
    app.router.add_post("/api/order", post_order)
    app.router.add_get(r"/api/order/{id:\d{1,9}}/payment", get_order_payment)
    app.router.add_post("/api/payment/notify", payment_notify)
    app.router.add_get(r"/pay/{id:\d{1,9}}/{sig:[0-9a-f]{24}}", pay_redirect)
    app.router.add_get("/api/checkout", get_checkout)
    app.router.add_get("/api/delivery/cdek/cities", get_cdek_cities)
    app.router.add_get("/api/delivery/cdek/points", get_cdek_points)
    app.router.add_post("/api/delivery/quote", post_quote)
    app.router.add_get("/api/me", get_me)
    app.router.add_post("/api/logout", post_logout)
    app.router.add_get("/auth/login", auth_login)
    app.router.add_get("/auth/callback", auth_callback)
    app.router.add_get(r"/{slug:" + "|".join(legal.PAGES) + "}", legal_page)
    for folder in ("assets", "css", "js"):
        app.router.add_static(f"/{folder}", settings.web_dir / folder)
    return app
