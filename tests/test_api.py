from urllib.parse import parse_qs, urlparse

from tests.conftest import ADMINS, FakeBot, FakeLogin, api_client, make_init_data, run

ORDER = {
    "surname": "Иванов", "name": "Иван", "patronymic": "Иванович", "phone": "(900) 123-45-67",
    "telegram": "@ivan", "email": "ivan@example.com", "delivery": "cdek", "address": "Казань, ул. Пушкина, 1",
    "payment": "qr", "comment": "", "cart": [{"id": 3, "size": "M", "quantity": 2, "price": 1}],
}


def get_orders(client, user_id):
    return client.get("/api/orders", headers={"X-TG-Init-Data": make_init_data(user_id)})


def post(client, body, user_id=1):
    return client.post("/api/order", json=body, headers={"X-TG-Init-Data": make_init_data(user_id)})


def test_catalog_and_static(settings):
    async def scenario():
        async with api_client(settings, FakeBot()) as (client, _):
            data = await (await client.get("/api/catalog")).json()
            assert [p["id"] for p in data["products"]] == [1, 2, 3, 4]
            page = await client.get("/")
            assert page.status == 200 and "text/html" in page.content_type
            assert page.headers["Cache-Control"] == "no-cache"
            assert (await client.get("/js/app.js")).status == 200
            assert (await client.get("/assets/../app/config.py")).status in (403, 404)
    run(scenario())


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
            assert body["order"]["total"] == 2 * 2590  # цена из каталога, а не от клиента
            assert body["order"]["qr"].startswith("assets/qr/")
            assert {cid for cid, _ in bot.sent} == {*ADMINS, 1}

            again = await post(client, ORDER)
            assert again.status == 429

            orders = (await (await get_orders(client, 1)).json())["orders"]
            item = orders[0]["items"][0]
            assert len(orders) == 1 and (item["size"], item["qty"], item["price"]) == ("M", 2, 2590)
            assert (await (await get_orders(client, 2)).json())["orders"] == []  # чужие заказы не видны
            assert (await db.get_order(1)).username == "ivan"
    run(scenario())


def test_order_validation(settings):
    async def scenario():
        async with api_client(settings, FakeBot()) as (client, _):
            cases = [
                ({"cart": [{"id": 1, "size": "M", "quantity": 1}]}, "закончились"),  # нет в наличии
                ({"cart": [{"id": 3, "size": "XXL", "quantity": 1}]}, "Размер"),
                ({"cart": [{"id": 3, "size": "M", "quantity": 99}]}, "Корзина"),
                ({"phone": "123"}, "Телефон"),
                ({"delivery": "<b>x</b>"}, "Доставка"),
            ]
            for uid, (patch, error) in enumerate(cases, 10):
                resp = await post(client, {**ORDER, **patch}, uid)
                assert resp.status == 400, patch
                assert error in (await resp.json())["description"]
    run(scenario())


def test_user_input_is_escaped_and_flood_alert(settings):
    bot = FakeBot()

    async def scenario():
        async with api_client(settings, bot) as (client, _):
            assert (await post(client, {**ORDER, "comment": "<script>x</script>"})).status == 200
            admin_text = next(t for cid, t in bot.sent if cid in ADMINS)
            assert "&lt;script&gt;" in admin_text and "<script>" not in admin_text

            bot.sent.clear()
            flood = {**ORDER, "cart": [{"id": 3, "size": "M", "quantity": 1}] * 31}
            assert (await post(client, flood, 5)).status == 400
            assert (await post(client, flood, 5)).status == 400
            assert len(bot.sent) == len(ADMINS)  # алерт один раз, без спама
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
            assert me == {"ok": True, "login_enabled": True, "user": None}
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
