from urllib.parse import parse_qs, urlparse

from app import api
from app.api import pay_link
from app.catalog import Catalog
from tests.conftest import ADMINS, FakeBot, FakeLogin, FakeYooKassa, api_client, make_init_data, run

ORDER = {
    "surname": "Иванов", "name": "Иван", "patronymic": "Иванович", "phone": "(900) 123-45-67",
    "telegram": "@ivan", "email": "ivan@example.com", "delivery": "cdek", "cdek_city_code": 424,
    "cdek_point": "KZN1", "comment": "", "cart": [{"id": 4, "size": "M", "quantity": 2, "price": 1}],
    "consent": True, "offer": True,
    "delivery_cost": 1, "total": 1,  # от клиента не принимаются
}


def get_orders(client, user_id):
    return client.get("/api/orders", headers={"X-TG-Init-Data": make_init_data(user_id)})


def post(client, body, user_id=1):
    return client.post("/api/order", json=body, headers={"X-TG-Init-Data": make_init_data(user_id)})


def notify(client, event, object_id):
    """Уведомление ЮKassa. Статусу в теле сервер не верит — спрашивает фейк по API, поэтому тело врёт."""
    body = {"type": "notification", "event": event, "object": {"id": object_id, "status": "succeeded"}}
    return client.post("/api/payment/notify", json=body)


def pay(client, payments, payment_id="pay-1", amount=None):
    """Покупатель оплатил: платёж у ЮKassa стал succeeded, пришло уведомление. amount — копейки."""
    payments.set(payment_id, "succeeded", amount)
    return notify(client, "payment.succeeded", payment_id)


def pay_path(settings, order_id=1):
    return urlparse(pay_link(settings, order_id)).path


def test_catalog_and_static(settings):
    async def scenario():
        async with api_client(settings, FakeBot()) as (client, _):
            data = await (await client.get("/api/catalog")).json()
            assert [p["id"] for p in data["products"]] == [1, 2, 4]
            # версии для миниатюр и зума всегда той же длины, что и список фото
            assert all(len(p["thumbs"]) == len(p["images"]) == len(p["zoom"]) for p in data["products"])
            page = await client.get("/")
            assert page.status == 200 and "text/html" in page.content_type
            assert page.headers["Cache-Control"] == "no-cache"
            assert (await client.get("/js/app.js")).status == 200
            assert (await client.get("/assets/../app/config.py")).status in (403, 404)
    run(scenario())


def test_photo_variants(tmp_path):
    """-sm/-full рядом с фото подхватываются, отсутствующие — заменяются оригиналом."""
    (tmp_path / "assets" / "p").mkdir(parents=True)
    for name in ("1.webp", "2.webp", "1-full.webp", "1-sm.webp"):
        (tmp_path / "assets" / "p" / name).write_bytes(b"")
    catalog = Catalog.model_validate({"products": [{
        "id": 1, "title": "Худи", "price": 100, "status": "in_stock", "sizes": ["S"],
        "images": ["assets/p/1.webp", "assets/p/2.webp"], "weight": 100,
    }]})
    catalog.attach_variants(tmp_path)
    product = catalog.get(1)
    assert product.thumbs == ["assets/p/1-sm.webp", "assets/p/2.webp"]
    assert product.zoom == ["assets/p/1-full.webp", "assets/p/2.webp"]


def test_auth_required(settings):
    async def scenario():
        async with api_client(settings, FakeBot()) as (client, _):
            assert (await client.get("/api/orders")).status == 401
            assert (await client.post("/api/order", json=ORDER)).status == 401
    run(scenario())


def test_order_flow(settings):
    bot = FakeBot()

    async def scenario():
        async with api_client(settings, bot) as (client, db):
            resp = await post(client, ORDER)
            body = await resp.json()
            assert resp.status == 200 and body["ok"]
            assert body["order"]["total"] == 2 * 2590 + 350  # цена из каталога, доставку посчитал сервер
            assert body["order"]["delivery_cost"] == 350 and body["order"]["payment_url"] == "https://pay.test/1"
            assert {cid for cid, _ in bot.sent} == {1}  # админам — только после оплаты

            again = await post(client, ORDER)
            assert again.status == 429

            orders = (await (await get_orders(client, 1)).json())["orders"]
            item = orders[0]["items"][0]
            assert len(orders) == 1 and (item["size"], item["qty"], item["price"]) == ("M", 2, 2590)
            assert (orders[0]["goods_total"], orders[0]["delivery_cost"], orders[0]["payment_status"],
                    orders[0]["payment_url"], orders[0]["address"]) == (
                5180, 350, "pending", "https://pay.test/1", "Казань, ул. Пушкина, 1")
            assert (await (await get_orders(client, 2)).json())["orders"] == []  # чужие заказы не видны
            order = await db.get_order(1)
            assert order.username == "ivan"
            assert order.consent_at and order.legal_version.startswith("согласие ")  # факт согласия сохранён
    run(scenario())


def test_order_requires_consent_and_offer(settings):
    async def scenario():
        async with api_client(settings, FakeBot()) as (client, db):
            for uid, patch in enumerate(({"consent": False}, {"offer": False}, {"consent": None}), 30):
                resp = await post(client, {**ORDER, **patch}, uid)
                assert resp.status == 400, patch
                assert "Согласие" in (await resp.json())["description"]
            no_fields = {k: v for k, v in ORDER.items() if k not in ("consent", "offer")}
            assert (await post(client, no_fields, 40)).status == 400
            assert await db.count_orders() == 0
    run(scenario())


def test_legal_pages(settings):
    async def scenario():
        async with api_client(settings, FakeBot()) as (client, _):
            for slug, words in (("offer", "Публичная оферта"), ("privacy", "Политика"), ("consent", "Согласие"),
                                ("requisites", "Реквизиты")):
                resp = await client.get(f"/{slug}")
                assert resp.status == 200 and "text/html" in resp.content_type, slug
                html = await resp.text()
                assert words in html and 'href="/offer"' in html  # навигация между документами
            assert (await client.get("/legal/offer.md")).status == 404  # исходники не отдаются
            assert (await client.get("/unknown")).status == 404
    run(scenario())


def test_order_validation(settings):
    async def scenario():
        async with api_client(settings, FakeBot()) as (client, _):
            cases = [
                ({"cart": [{"id": 1, "size": "M", "quantity": 1}]}, "закончились"),  # нет в наличии
                ({"cart": [{"id": 4, "size": "XXL", "quantity": 1}]}, "Размер"),
                ({"cart": [{"id": 4, "size": "M", "quantity": 99}]}, "Корзина"),
                ({"phone": "123"}, "Телефон"),
                ({"delivery": "<b>x</b>"}, "Доставка"),
            ]
            for uid, (patch, error) in enumerate(cases, 10):
                resp = await post(client, {**ORDER, **patch}, uid)
                assert resp.status == 400, patch
                assert error in (await resp.json())["description"]
    run(scenario())


def test_user_input_is_escaped_and_flood_alert(settings):
    bot, payments = FakeBot(), FakeYooKassa()

    async def scenario():
        async with api_client(settings, bot, payments=payments) as (client, _):
            assert (await post(client, {**ORDER, "comment": "<script>x</script>"})).status == 200
            assert (await pay(client, payments)).status == 200
            admin_text = next(t for cid, t in bot.sent if cid in ADMINS)
            assert "&lt;script&gt;" in admin_text and "<script>" not in admin_text

            bot.sent.clear()
            flood = {**ORDER, "cart": [{"id": 4, "size": "M", "quantity": 1}] * 31}
            assert (await post(client, flood, 5)).status == 400
            assert (await post(client, flood, 5)).status == 400
    run(scenario())


# --- вход на сайт через Telegram ---

async def site_login(client, next_screen="orders", code="ok"):
    """Проходит вход как браузер: /auth/login → (Telegram) → /auth/callback. Возвращает ответ callback."""
    start = await client.get(f"/auth/login?next={next_screen}", allow_redirects=False)
    assert start.status == 302 and start.headers["Cache-Control"] == "no-store"
    state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]
    return await client.get(f"/auth/callback?code={code}&state={state}", allow_redirects=False)


def test_site_login_orders_and_logout(settings):
    bot, login = FakeBot(), FakeLogin()

    async def scenario():
        async with api_client(settings, bot, login) as (client, db):
            me = await (await client.get("/api/me")).json()
            assert me == {"ok": True, "login_enabled": True, "bot_username": "morelufs_test_bot", "user": None}
            assert (await client.get("/api/orders")).status == 401

            done = await site_login(client, "checkout")
            assert done.status == 302 and done.headers["Location"] == "/?login=ok&next=checkout"
            session_cookie = next(c for c in done.headers.getall("Set-Cookie") if c.startswith("mls_session="))
            assert "HttpOnly" in session_cookie and "SameSite=Lax" in session_cookie
            assert {cid for cid, _ in bot.sent} == ADMINS  # админам — «новый пользователь»

            me = (await (await client.get("/api/me")).json())["user"]
            assert (me["id"], me["username"], me["phone"], me["via"]) == (1, "ivan", "(900) 123-45-67", "site")

            # заказ с сайта: тот же id, что в мини-аппе, — заказ виден в обоих местах
            resp = await client.post("/api/order", json=ORDER, headers={"Origin": str(client.make_url("/"))[:-1]})
            assert resp.status == 200, await resp.text()
            assert len((await (await client.get("/api/orders")).json())["orders"]) == 1
            assert len((await (await get_orders(client, 1)).json())["orders"]) == 1

            # POST с чужого сайта с нашей cookie не проходит
            evil = await client.post("/api/order", json=ORDER, headers={"Origin": "https://evil.example"})
            assert evil.status == 403

            assert (await client.post("/api/logout")).status == 200
            assert (await (await client.get("/api/me")).json())["user"] is None
            assert (await client.get("/api/orders")).status == 401
            assert await db.delete_user_sessions(1) == 0
    run(scenario())


def test_site_login_rejects_foreign_or_reused_state(settings):
    login = FakeLogin()

    async def scenario():
        async with api_client(settings, FakeBot(), login) as (client, _):
            start = await client.get("/auth/login", allow_redirects=False)
            state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]
            client.session.cookie_jar.clear()  # чужой браузер: ссылка та же, cookie нет
            foreign = await client.get(f"/auth/callback?code=ok&state={state}", allow_redirects=False)
            assert foreign.headers["Location"] == "/?login=expired"
            again = await client.get(f"/auth/callback?code=ok&state={state}", allow_redirects=False)
            assert again.headers["Location"] == "/?login=expired"  # state одноразовый
            assert login.exchanged == []  # до обмена кода не дошло

            assert (await site_login(client, code="bad")).headers["Location"] == "/?login=failed&next=orders"
            cancelled = await client.get("/auth/callback?error=access_denied", allow_redirects=False)
            assert cancelled.headers["Location"] == "/?login=cancelled"
            assert (await (await client.get("/api/me")).json())["user"] is None
    run(scenario())


def test_login_disabled_without_client_id(settings):
    async def scenario():
        async with api_client(settings, FakeBot()) as (client, _):
            assert (await (await client.get("/api/me")).json())["login_enabled"] is False
            resp = await client.get("/auth/login", allow_redirects=False)
            assert resp.headers["Location"] == "/?login=disabled"
    run(scenario())


def test_miniapp_sees_phone_from_site_login(settings):
    async def scenario():
        async with api_client(settings, FakeBot(), FakeLogin()) as (client, _):
            await site_login(client)
            client.session.cookie_jar.clear()
            me = await client.get("/api/me", headers={"X-TG-Init-Data": make_init_data(1)})
            user = (await me.json())["user"]
            assert (user["via"], user["phone"]) == ("miniapp", "(900) 123-45-67")
    run(scenario())


# --- бот не может написать первым: «!» в корзине ---

def me_as(client, user_id):
    return client.get("/api/me", headers={"X-TG-Init-Data": make_init_data(user_id)})


def test_needs_bot_start_after_undelivered_order(settings):
    bot = FakeBot(unreachable={7})

    async def scenario():
        async with api_client(settings, bot) as (client, db):
            me = await (await me_as(client, 7)).json()
            assert me["bot_username"] == "morelufs_test_bot" and me["user"]["needs_bot_start"] is False

            body = await (await post(client, ORDER, user_id=7)).json()
            assert body["ok"] and body["order"]["notified"] is False  # заказ создан, но сообщение не дошло
            assert (await (await me_as(client, 7)).json())["user"]["needs_bot_start"] is True

            await db.set_bot_dm(7, True)  # так делает /start у бота
            assert (await (await me_as(client, 7)).json())["user"]["needs_bot_start"] is False
    run(scenario())


def test_site_login_rechecks_bot_access(settings):
    bot = FakeBot(unreachable={1})

    async def scenario():
        async with api_client(settings, bot, FakeLogin()) as (client, db):
            assert (await (await post(client, ORDER, user_id=1)).json())["order"]["notified"] is False
            bot.unreachable.clear()  # при входе на сайт человек разрешил боту писать
            await site_login(client)
            me = (await (await client.get("/api/me")).json())["user"]
            assert me["via"] == "site" and me["needs_bot_start"] is False
            assert (await db.get_user(1))["bot_dm"] == 1
    run(scenario())


# --- оформление: ЮKassa, СДЭК, Почта ---

POCHTA_ORDER = {**ORDER, "delivery": "pochta", "address": "Казань, Баумана 1-5", "cdek_city_code": None,
                "cdek_point": None}


def test_checkout_flags(settings):
    async def scenario():
        cases = [
            ({}, True, {"cdek": True, "pochta": True}),
            ({"payments": None}, False, {"cdek": True, "pochta": True}),
            ({"cdek": None, "pochta": None}, False, {"cdek": False, "pochta": False}),
            ({"pochta": None}, True, {"cdek": True, "pochta": False}),
        ]
        for clients, enabled, delivery in cases:
            async with api_client(settings, FakeBot(), **clients) as (client, _):
                resp = await client.get("/api/checkout")
                body = await resp.json()
                assert (body["enabled"], body["delivery"]) == (enabled, delivery), clients
                assert (body["message"] is None) == enabled
    run(scenario())


def test_settings_service_flags(settings):
    from dataclasses import replace
    assert not settings.checkout_enabled
    full = replace(settings, yookassa_shop_id="1", yookassa_secret_key="k", cdek_client_id="c", cdek_client_secret="s")
    assert full.payments_enabled and full.cdek_enabled and full.checkout_enabled and not full.pochta_enabled
    assert not replace(full, cdek_client_secret="").checkout_enabled
    pochta = replace(settings, pochta_token="t", pochta_login="l", pochta_password="p")
    assert not pochta.pochta_enabled  # без индекса отправителя Почта не посчитает тариф
    assert replace(pochta, pochta_from_index="420000").pochta_enabled


def test_order_without_keys_is_disabled(settings):
    async def scenario():
        async with api_client(settings, FakeBot(), payments=None, cdek=None, pochta=None) as (client, db):
            resp = await post(client, ORDER)
            assert resp.status == 503 and "недоступно" in (await resp.json())["description"]
            assert (await client.get("/api/delivery/cdek/cities?q=Каз")).status == 503
            assert await db.count_orders() == 0
            assert (await notify(client, "payment.succeeded", "pay-1")).status == 404
            assert (await client.get(pay_path(settings))).status == 404  # заказа нет
    run(scenario())


def test_cdek_order_details_and_pay_button(settings):
    from tests.conftest import FakeCdek
    bot, cdek, payments = FakeBot(), FakeCdek(), FakeYooKassa()

    async def scenario():
        async with api_client(settings, bot, cdek=cdek, payments=payments) as (client, db):
            resp = await post(client, ORDER)
            assert resp.status == 200 and (await resp.json())["order"]["payment_url"] == "https://pay.test/1"
            (city, parcel), = cdek.quotes
            assert city == 424 and parcel.weight_g == 2 * 350 + 150  # лонгсливы + упаковка
            create = payments.creates[0]
            assert create["amount"] == 5530 and create["order_id"] == 1 and create["phone"] == "+79001234567"
            link = pay_link(settings, 1)
            assert link.startswith("http://example.test/pay/1/") and create["return_url"] == link + "?return=1"
            assert create["receipt"] is None  # YOOKASSA_VAT_CODE не задан — чек не передаём
            (uid, markup), = bot.markups
            assert uid == 1 and markup.inline_keyboard[0][0].url == link  # кнопка живёт сутки, а не как платёж
            order = await db.get_order(1)
            assert (order.status, order.payment, order.payment_status, order.payment_id, order.payment_url) == (
                "Ожидает оплаты", "yookassa", "pending", "pay-1", "https://pay.test/1")
            assert order.delivery_info["cdek_point"] == "KZN1" and order.delivery_info["days_max"] == 4
    run(scenario())


def test_receipt_when_vat_code_set(settings):
    from dataclasses import replace
    payments = FakeYooKassa()

    async def scenario():
        async with api_client(replace(settings, yookassa_vat_code=1), FakeBot(), payments=payments) as (client, _):
            assert (await post(client, ORDER)).status == 200
            lines = payments.creates[0]["receipt"]
            assert [(r.price, r.qty, r.payment_subject) for r in lines] == [(2590, 2, "commodity"), (350, 1, "service")]
    run(scenario())


def test_pochta_order_and_bad_address(settings):
    from tests.conftest import FakePochta
    pochta = FakePochta()

    async def scenario():
        async with api_client(settings, FakeBot(), pochta=pochta) as (client, db):
            bad = await post(client, {**POCHTA_ORDER, "address": "плохой адрес"})
            assert bad.status == 400 and "индекс" in (await bad.json())["description"]
            assert await db.count_orders() == 0
            resp = await post(client, POCHTA_ORDER)  # после ошибки доставки ждать 10 секунд не нужно
            body = await resp.json()
            assert resp.status == 200 and body["order"]["total"] == 5180 + 410
            assert pochta.quotes[-1][0] == "420111" and pochta.quotes[-1][2] == 0  # без объявленной ценности
            order = await db.get_order(1)
            assert order.address.startswith("420111, ") and order.delivery_info["post_address"]["index"] == "420111"
            assert (await post(client, {**POCHTA_ORDER, "address": ""}, 2)).status == 400
    run(scenario())


def test_delivery_errors(settings):
    async def scenario():
        async with api_client(settings, FakeBot(), pochta=None) as (client, db):
            cases = [
                ({"cdek_point": "NOPE"}, "Пункт выдачи СДЭК не найден"),
                ({"cdek_city_code": 44}, "Пункт выдачи СДЭК не найден"),  # ПВЗ из другого города
                ({"cdek_point": None}, "Выберите город и пункт выдачи СДЭК"),
                ({"cdek_point": "<b>"}, "Пункт выдачи СДЭК"),
                ({"delivery": "pochta", "address": "Казань"}, "Почтой России сейчас недоступна"),
            ]
            for uid, (patch, error) in enumerate(cases, 50):
                resp = await post(client, {**ORDER, **patch}, uid)
                assert resp.status == 400, patch
                assert error in (await resp.json())["description"], patch
            assert await db.count_orders() == 0
    run(scenario())


def test_payment_init_failure_keeps_no_order(settings):
    bot = FakeBot()

    async def scenario():
        async with api_client(settings, bot, payments=FakeYooKassa(fail=True)) as (client, db):
            resp = await post(client, ORDER)
            assert resp.status == 502 and "платёж" in (await resp.json())["description"]
            assert await db.count_orders() == 0 and bot.sent == []
            assert (await post(client, ORDER)).status == 502  # не 429: повторить можно сразу
    run(scenario())


def test_quote_and_cdek_lookups(settings):
    async def scenario():
        async with api_client(settings, FakeBot()) as (client, _):
            body = {"cart": ORDER["cart"], "delivery": "cdek", "cdek_city_code": 424, "cdek_point": "KZN1"}
            resp = await client.post("/api/delivery/quote", json=body)  # гостю тоже можно
            assert await resp.json() == {"ok": True, "delivery_cost": 350, "days_min": 2, "days_max": 4,
                                         "goods_total": 5180, "total": 5530, "address": "Казань, ул. Пушкина, 1"}
            assert (await client.post("/api/delivery/quote", json={**body, "cdek_point": "X"})).status == 400
            assert (await client.post("/api/delivery/quote", json={**body, "cart": []})).status == 400

            cities = await (await client.get("/api/delivery/cdek/cities?q=Каз")).json()
            assert cities == {"ok": True, "cities": [{"code": 424, "name": "Казань", "region": "Республика Татарстан"}]}
            assert (await (await client.get("/api/delivery/cdek/cities?q=К")).json())["cities"] == []
            assert (await client.get("/api/delivery/cdek/cities?q=" + "а" * 65)).status == 400
            points = (await (await client.get("/api/delivery/cdek/points?city=424")).json())["points"]
            assert points[0]["code"] == "KZN1" and points[0]["latitude"] == 55.79
            for bad in ("abc", "-1", "", "99999999999"):
                assert (await client.get(f"/api/delivery/cdek/points?city={bad}")).status == 400, bad
    run(scenario())


def test_payment_webhook(settings):
    bot, payments = FakeBot(), FakeYooKassa()
    api._alerted.clear()

    async def scenario():
        async with api_client(settings, bot, payments=payments) as (client, db):
            assert (await post(client, ORDER)).status == 200
            bot.sent.clear()
            fake = await notify(client, "payment.succeeded", "pay-1")  # тело врёт: у ЮKassa платёж ещё pending
            assert fake.status == 200 and (await db.get_order(1)).payment_status == "pending"
            wrong = await pay(client, payments, amount=100)
            assert wrong.status == 200 and await wrong.text() == "OK"
            assert (await db.get_order(1)).payment_status == "pending"  # сумма не сошлась — не оплачен
            assert any("не равную заказу" in t for cid, t in bot.sent if cid in ADMINS)  # админ предупреждён

            payments.set("pay-1", "succeeded", 553000)
            for _ in range(2):  # ЮKassa может повторить уведомление
                resp = await notify(client, "payment.succeeded", "pay-1")
                assert resp.status == 200 and await resp.text() == "OK"
            order = await db.get_order(1)
            assert (order.status, order.payment_status) == ("Оплачен", "paid") and order.paid_at
            admin_msgs = [t for cid, t in bot.sent if cid in ADMINS and "ОПЛАЧЕН" in t]
            assert len(admin_msgs) == len(ADMINS) and "ОПЛАЧЕН ЗАКАЗ #1" in admin_msgs[0]
            assert [t for cid, t in bot.sent if cid == 1] == [bot.sent[-1][1]] and "Оплата" in bot.sent[-1][1]

            # неизвестный ЮKassa платёж — 502, чтобы настоящее уведомление повторилось, а не потерялось
            assert (await notify(client, "payment.succeeded", "pay-404")).status == 502
            assert (await client.post("/api/payment/notify", data="not json")).status == 400
            assert (await notify(client, "payment.succeeded", "../refunds")).status == 400
            assert (await notify(client, "payout.succeeded", "po-1")).status == 200  # лишние события — игнор

            refund = await payments.refund("pay-1", 5530)  # вернули из личного кабинета
            await notify(client, "refund.succeeded", refund.id)
            order = await db.get_order(1)
            assert (order.status, order.payment_status) == ("Отменён, деньги возвращены", "refunded")
    run(scenario())


def test_canceled_payment_and_pay_link(settings):
    bot, payments = FakeBot(), FakeYooKassa()
    api._alerted.clear()

    async def scenario():
        async with api_client(settings, bot, payments=payments) as (client, db):
            await post(client, ORDER)
            path = pay_path(settings)
            resp = await client.get(path, allow_redirects=False)
            assert resp.status == 302 and resp.headers["Location"] == "https://pay.test/1"
            assert resp.headers["Cache-Control"] == "no-store"
            assert (await client.get(path.replace("/pay/1/", "/pay/2/"), allow_redirects=False)).status == 404

            payments.set("pay-1", "canceled")  # банк отказал — ЮKassa отменила платёж
            await notify(client, "payment.canceled", "pay-1")
            order = await db.get_order(1)
            assert (order.status, order.payment_status) == ("Ожидает оплаты", "pending")  # на оплату — сутки
            orders = (await (await get_orders(client, 1)).json())["orders"]
            assert orders[0]["payment_url"] == pay_link(settings, 1)
            for _ in range(2):  # повторный клик ведёт на тот же новый платёж
                resp = await client.get(path, allow_redirects=False)
                assert resp.headers["Location"] == "https://pay.test/2"
            assert len(payments.creates) == 2 and (await db.get_order(1)).payment_id == "pay-2"
            back = await client.get(path + "?return=1", allow_redirects=False)  # ушли со страницы, не заплатив
            assert back.headers["Location"] == "http://example.test/?payment=fail&order=1"

            # заплатили по старой странице, когда уже создан новый платёж, — деньги засчитываются
            payments.set("pay-1", "succeeded")
            await notify(client, "payment.succeeded", "pay-1")
            order = await db.get_order(1)
            assert order.payment_status == "paid" and order.payment_id == "pay-1"
            for url in (path, path + "?return=1"):
                resp = await client.get(url, allow_redirects=False)
                assert resp.headers["Location"] == "http://example.test/?payment=success&order=1"

            bot.sent.clear()  # второй платёж тоже прошёл — заказ не трогаем, админу предупреждение
            await pay(client, payments, "pay-2")
            assert (await db.get_order(1)).payment_id == "pay-1"
            assert any("оплачен второй раз" in t for cid, t in bot.sent if cid in ADMINS)
    run(scenario())


def test_pay_link_when_kassa_is_down(settings):
    payments = FakeYooKassa()

    async def scenario():
        async with api_client(settings, FakeBot(), payments=payments) as (client, db):
            await post(client, ORDER)
            payments.payments.clear()  # ЮKassa отвечает ошибкой
            resp = await client.get(pay_path(settings), allow_redirects=False)
            assert resp.status == 502 and "недоступен" in await resp.text()
            await db.conn.execute("UPDATE orders SET created_at = datetime('now', '-25 hours') WHERE id = 1")
            await db.conn.commit()
            orders = (await (await get_orders(client, 1)).json())["orders"]
            assert orders[0]["payment_url"] is None  # сутки прошли — платить уже нельзя
    run(scenario())


def test_order_payment_status_sync(settings):
    bot, payments = FakeBot(), FakeYooKassa()

    async def scenario():
        async with api_client(settings, bot, payments=payments) as (client, db):
            await post(client, ORDER)
            assert (await client.get("/api/order/1/payment")).status == 401
            foreign = await client.get("/api/order/1/payment", headers={"X-TG-Init-Data": make_init_data(2)})
            assert foreign.status == 404
            missing = await client.get("/api/order/99/payment", headers={"X-TG-Init-Data": make_init_data(1)})
            assert missing.status == 404

            resp = await client.get("/api/order/1/payment", headers={"X-TG-Init-Data": make_init_data(1)})
            assert resp.headers["Cache-Control"] == "no-store"
            assert await resp.json() == {"ok": True, "id": 1, "payment_status": "pending", "status": "Ожидает оплаты",
                                         "total": 5530, "payment_url": pay_link(settings, 1)}
            payments.set("pay-1", "succeeded")  # уведомление ещё не пришло, а ЮKassa уже знает
            mine = {"X-TG-Init-Data": make_init_data(1)}
            body = await (await client.get("/api/order/1/payment", headers=mine)).json()
            assert (body["payment_status"], body["status"], body["payment_url"]) == ("paid", "Оплачен", None)
            assert len([cid for cid, _ in bot.sent if cid in ADMINS]) == len(ADMINS)
            await notify(client, "payment.succeeded", "pay-1")  # опоздавшее уведомление — без повторов
            assert len([cid for cid, _ in bot.sent if cid in ADMINS]) == len(ADMINS)
    run(scenario())


def test_expire_unpaid_orders(settings):
    from app.__main__ import expire_unpaid
    bot, payments = FakeBot(), FakeYooKassa()

    async def scenario():
        async with api_client(settings, bot, payments=payments) as (client, db):
            for uid in (1, 2, 3):
                assert (await post(client, ORDER, uid)).status == 200
            await db.conn.execute("UPDATE orders SET created_at = datetime('now', '-25 hours') WHERE id IN (1, 2)")
            await db.conn.commit()
            bot.sent.clear()
            assert await expire_unpaid(bot, db, settings, payments) == 2
            for oid in (1, 2):
                order = await db.get_order(oid)
                assert (order.status, order.payment_status) == ("Отменён: не оплачен", "expired")
            assert (await db.get_order(3)).payment_status == "pending"  # свежий заказ не трогаем
            assert {cid for cid, _ in bot.sent} == {1, 2}
            assert await expire_unpaid(bot, db, settings, payments) == 0

            # уведомление об оплате потерялось: перед отменой сверяемся с ЮKassa
            await db.conn.execute("UPDATE orders SET created_at = datetime('now', '-25 hours') WHERE id = 3")
            await db.conn.commit()
            payments.set("pay-3", "succeeded")
            assert await expire_unpaid(bot, db, settings, payments) == 0
            assert (await db.get_order(3)).payment_status == "paid"
    run(scenario())


# --- бот: кнопки карточки заказа, отправления, возврат ---

def test_admin_card_shipment_and_refund(settings):
    from app.bot import admin
    from app.bot import keyboards as kb
    from app.texts import order_for_admin
    from tests.conftest import FakeCdek
    bot, cdek, payments = FakeBot(), FakeCdek(), FakeYooKassa()

    def buttons(order, svc):
        return [b.text for row in kb.order_card(order, 0, svc).inline_keyboard for b in row]

    async def scenario():
        async with api_client(settings, bot, cdek=cdek, payments=payments) as (client, db):
            await post(client, ORDER)
            svc = admin.services(payments, cdek, None)
            order = await db.get_order(1)
            assert not any(t.startswith(("🚚", "🔄", "💸")) for t in buttons(order, svc))  # ещё не оплачен
            await pay(client, payments)
            order = await db.get_order(1)
            text = order_for_admin(order)
            assert "KZN1" in text and "pay-1" in text and "350 руб." in text and "оплачен" in text
            assert buttons(order, svc)[:2] == ["🚚 Создать отправление", "💸 Вернуть деньги"]
            assert not any(t.startswith(("🚚", "💸")) for t in buttons(order, frozenset()))  # сервисы не настроены

            shipment = admin.shipment_for(order)
            assert (shipment.order_number, shipment.recipient.phone, shipment.cdek_point) == ("ML-1", "+79001234567",
                                                                                              "KZN1")
            assert shipment.items[0].weight_g == 350 and shipment.declared_value == 5180

            cdek.track = None  # СДЭК присваивает номер не сразу
            order, note = await admin.create_shipment(bot, db, order, cdek)
            assert order.shipment_id == "cdek-uuid-1" and order.track is None and "ещё не присвоен" in note
            assert buttons(order, svc)[0] == "🔄 Получить трек-номер"
            assert (await admin.create_shipment(bot, db, order, cdek))[0] is order and len(cdek.shipments) == 1

            cdek.track = "1234567890"
            bot.sent.clear()
            order, _ = await admin.fetch_track(bot, db, order, cdek)
            assert order.track == "1234567890" and bot.sent[0][0] == 1 and "1234567890" in bot.sent[0][1]

            payments.refund_status = "pending"  # ЮKassa приняла возврат, но ещё не провела
            order, note = await admin.refund(bot, db, order, payments)
            assert order.payment_status == "paid" and "обрабатывается" in note
            payments.refund_status = "succeeded"
            order, note = await admin.refund(bot, db, order, payments)
            assert (order.status, order.payment_status) == ("Отменён, деньги возвращены", "refunded")
            assert payments.refunds == [("pay-1", 5530)] * 2 and "деньги возвращены" in bot.sent[-1][1]
            assert "Вернуть" not in " ".join(buttons(order, svc))
    run(scenario())


def test_admin_delete_order_notifies_customer(settings):
    from app.bot import admin
    from app.bot import keyboards as kb
    from app.texts import delete_prompt
    from tests.conftest import FakeCdek
    bot, cdek, payments = FakeBot(), FakeCdek(), FakeYooKassa()

    async def scenario():
        async with api_client(settings, bot, cdek=cdek, payments=payments) as (client, db):
            for uid in (1, 2):
                await post(client, ORDER, uid)
            order = await db.get_order(1)
            assert "оплачен" not in delete_prompt(order) and "отправление" not in delete_prompt(order)
            texts_ = [b.text for row in kb.confirm_delete(order, 0).inline_keyboard for b in row]
            assert texts_ == ["Да, удалить без комментария", "Да, удалить с комментарием", "Отмена"]

            bot.sent.clear()
            note = await admin.remove_order(bot, db, order)
            assert await db.get_order(1) is None and note == "Покупатель уведомлён."
            assert bot.sent == [(1, bot.sent[0][1])] and "Заказ #1 удалён" in bot.sent[0][1]
            assert "Комментарий" not in bot.sent[0][1]
            assert await admin.remove_order(bot, db, order) == "Заказ уже удалён."

            await pay(client, payments, "pay-2")
            order, _ = await admin.create_shipment(bot, db, await db.get_order(2), cdek)
            prompt = delete_prompt(order)
            assert "удаление не вернёт деньги" in prompt and "cdek-uuid-1" in prompt
            bot.sent.clear()
            note = await admin.remove_order(bot, db, order, "Нет размера <M>")
            assert "cdek-uuid-1" in note
            assert "Комментарий менеджера:</b>\nНет размера &lt;M&gt;" in bot.sent[0][1] and bot.sent[0][0] == 2
    run(scenario())
