import base64
import contextlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.delivery import DeliveryError, Item, Parcel, PostAddress, Quote, Recipient, Shipment
from app.delivery import cdek as cdek_module
from app.delivery.cdek import CdekClient
from app.delivery.pochta import PochtaClient
from tests.conftest import run

PARCEL = Parcel(1200, 30, 20, 10)
RECIPIENT = Recipient("Иванов", "Иван", "Иванович", "+79001234567", "ivan@example.com")
ITEMS = [Item("Худи", "M", 2, 4990, 500)]


class Fake:
    """Фейковый API: handlers[(метод, путь)] -> (статус, json); всё полученное — в calls."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: list[dict] = []

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.post() if request.content_type.endswith("urlencoded") else (
            await request.json() if request.can_read_body else None)
        self.calls.append({"method": request.method, "path": request.path, "query": dict(request.query),
                           "headers": dict(request.headers), "body": body})
        answer = self.handlers.get((request.method, request.path))
        if answer is None:
            return web.json_response({"errors": [{"code": "not_found", "message": "нет"}]}, status=404)
        status, data = answer(request) if callable(answer) else answer
        if isinstance(data, str):
            return web.Response(status=status, text=data)
        return web.json_response(data, status=status)

    def paths(self, path: str) -> list[dict]:
        return [c for c in self.calls if c["path"] == path]


@contextlib.asynccontextmanager
async def fake_server(handlers: dict):
    fake = Fake(handlers)
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    async with TestServer(app) as server:
        yield fake, str(server.make_url(""))


TOKEN_OK = (200, {"access_token": "tok-1", "token_type": "bearer", "expires_in": 3600})


def cdek_handlers(extra: dict | None = None) -> dict:
    return {("POST", "/v2/oauth/token"): TOKEN_OK, **(extra or {})}


@contextlib.asynccontextmanager
async def cdek_client(handlers: dict, **kwargs):
    async with fake_server(handlers) as (fake, url):
        client = CdekClient("id", "secret", base_url=url + "/v2", **kwargs)
        try:
            yield fake, client
        finally:
            await client.close()


# ---------- СДЭК ----------

def test_cdek_token_cached_and_cities():
    cities = [
        {"city_uuid": "u1", "code": 424, "full_name": "Казань, Татарстан респ., Россия", "country_code": "RU"},
        {"city_uuid": "u2", "code": 9999, "full_name": "Казанская, Краснодарский край, Россия"},
        {"code": "bad"},
    ]

    async def go():
        async with cdek_client(cdek_handlers({("GET", "/v2/location/suggest/cities"): (200, cities)})) as (f, c):
            got = await c.cities(" Каз ")
            assert [(x.code, x.name, x.region) for x in got] == [
                (424, "Казань", "Татарстан респ."), (9999, "Казанская", "Краснодарский край")]
            assert len(await c.cities("Каз", limit=1)) == 1
            assert await c.cities("  ") == []
            tokens = f.paths("/v2/oauth/token")
            assert len(tokens) == 1  # второй запрос — с кэшированным токеном
            assert tokens[0]["body"] == {"grant_type": "client_credentials", "client_id": "id",
                                         "client_secret": "secret"}
            search = f.paths("/v2/location/suggest/cities")
            assert search[0]["query"] == {"name": "Каз", "country_code": "RU"}
            assert all(s["headers"]["Authorization"] == "Bearer tok-1" for s in search)

    run(go())


def test_cdek_token_refresh_on_expiry_and_401():
    state = {"n": 0}

    def token(_):
        state["n"] += 1
        return 200, {"access_token": f"tok-{state['n']}", "expires_in": 30}  # меньше запаса — не кэшируется

    def cities(request):
        return (200, []) if request.headers["Authorization"] == "Bearer tok-3" else (401, {})

    async def go():
        handlers = {("POST", "/v2/oauth/token"): token, ("GET", "/v2/location/suggest/cities"): cities}
        async with cdek_client(handlers) as (f, c):
            # tok-1 → 401 → tok-2 → 401 → ошибка; затем tok-3 проходит
            with pytest.raises(DeliveryError, match="временно недоступна"):
                await c.cities("Каз")
            assert await c.cities("Каз") == []
            assert len(f.paths("/v2/oauth/token")) == 3

    run(go())


def test_cdek_bad_credentials():
    async def go():
        handlers = {("POST", "/v2/oauth/token"): (401, {"error": "invalid_client"})}
        async with cdek_client(handlers) as (_, c):
            with pytest.raises(DeliveryError, match="временно недоступна"):
                await c.cities("Каз")

    run(go())


POINTS = [
    {"code": "KZN1", "name": "На Баумана", "type": "PVZ", "work_time": "Пн-Вс 10:00-20:00", "phones": [],
     "location": {"address": "ул. Баумана, 1", "address_full": "Россия, Казань, ул. Баумана, 1",
                  "latitude": 55.79, "longitude": 49.11}},
    {"code": "KZN2", "name": "Второй", "location": {"address_full": "Казань, ул. Пушкина, 2"}},
    {"name": "без кода"},
]


def test_cdek_points_cache_and_point():
    async def go():
        async with cdek_client(cdek_handlers({("GET", "/v2/deliverypoints"): (200, POINTS)})) as (f, c):
            points = await c.points(424)
            assert [p.code for p in points] == ["KZN1", "KZN2"]
            p = points[0]
            assert (p.name, p.address, p.work_time, p.latitude, p.longitude) == (
                "На Баумана", "Россия, Казань, ул. Баумана, 1", "Пн-Вс 10:00-20:00", 55.79, 49.11)
            assert points[1].address == "Казань, ул. Пушкина, 2" and points[1].latitude is None
            assert (await c.point(424, "KZN2")).name == "Второй"
            assert await c.point(424, "NOPE") is None
            assert len(f.paths("/v2/deliverypoints")) == 1  # остальное — из кэша
            assert f.calls[-1]["query"] == {"city_code": "424", "type": "PVZ", "is_handout": "true"}

            await c.points(137)  # другой город — отдельный запрос
            assert len(f.paths("/v2/deliverypoints")) == 2

            # через 6 часов кэш устаревает (time.monotonic не подменяем — на нём работает event loop)
            stamp, cached = c._points[424]
            c._points[424] = (stamp - cdek_module.POINTS_TTL - 1, cached)
            await c.points(424)
            assert len(f.paths("/v2/deliverypoints")) == 3

    run(go())


def test_cdek_quote():
    ok = {"delivery_sum": 300.0, "period_min": 2, "period_max": 4, "total_sum": 345.01, "currency": "RUB"}

    async def go():
        async with cdek_client(cdek_handlers({("POST", "/v2/calculator/tariff"): (200, ok)})) as (f, c):
            q = await c.quote(44, PARCEL)
            assert (q.cost, q.days_min, q.days_max) == (346, 2, 4)  # копейки — вверх до рубля
            assert f.calls[-1]["body"] == {
                "type": 1, "tariff_code": 136, "from_location": {"code": 424}, "to_location": {"code": 44},
                "packages": [{"weight": 1200, "length": 30, "width": 20, "height": 10}]}

        err = {"errors": [{"code": "ERR_TARIFF", "message": "Тариф недоступен"}]}
        for answer in ((400, err), (200, err)):
            async with cdek_client(cdek_handlers({("POST", "/v2/calculator/tariff"): answer})) as (_, c):
                with pytest.raises(DeliveryError, match="не доставляет"):
                    await c.quote(44, PARCEL)
        for answer in ((500, {}), (200, "<html>"), (200, {"period_min": 1})):
            async with cdek_client(cdek_handlers({("POST", "/v2/calculator/tariff"): answer})) as (_, c):
                with pytest.raises(DeliveryError, match="временно недоступна"):
                    await c.quote(44, PARCEL)

    run(go())


def cdek_shipment(point: str | None = "KZN1") -> Shipment:
    return Shipment("ML-15", RECIPIENT, PARCEL, ITEMS, declared_value=9980, cdek_city_code=424, cdek_point=point)


def test_cdek_create_and_track():
    created = {"entity": {"uuid": "uuid-1"}, "requests": [{"request_uuid": "r1", "type": "CREATE",
                                                          "state": "ACCEPTED", "errors": []}]}
    track = {"pending": (200, {"entity": {"uuid": "uuid-1"}, "requests": [{"type": "CREATE", "state": "SUCCESSFUL"}]})}

    def order(_):
        return track["pending"]

    async def go():
        handlers = cdek_handlers({("POST", "/v2/orders"): (202, created), ("GET", "/v2/orders/uuid-1"): order})
        async with cdek_client(handlers, shipment_point="KZN99") as (f, c):
            assert await c.create_shipment(cdek_shipment()) == "uuid-1"
            body = f.paths("/v2/orders")[0]["body"]
            assert body["type"] == 1 and body["number"] == "ML-15" and body["tariff_code"] == 136
            assert body["shipment_point"] == "KZN99" and "from_location" not in body
            assert body["delivery_point"] == "KZN1"
            assert body["recipient"] == {"name": "Иванов Иван Иванович", "phones": [{"number": "+79001234567"}],
                                         "email": "ivan@example.com"}
            pkg = body["packages"][0]
            assert (pkg["number"], pkg["weight"], pkg["length"]) == ("1", 1200, 30)
            assert pkg["items"] == [{"name": "Худи, размер M", "ware_key": "Худи M", "payment": {"value": 0},
                                     "cost": 4990, "weight": 500, "amount": 2}]

            assert await c.track_number("uuid-1") is None  # номер присваивается не сразу
            track["pending"] = (200, {"entity": {"uuid": "uuid-1", "cdek_number": "1106207236"}, "requests": []})
            assert await c.track_number("uuid-1") == "1106207236"
            track["pending"] = (200, {"entity": {"uuid": "uuid-1"}, "requests": [
                {"type": "CREATE", "state": "INVALID", "errors": [{"code": "x", "message": "Неверный ПВЗ"}]}]})
            with pytest.raises(DeliveryError, match="Неверный ПВЗ"):
                await c.track_number("uuid-1")
            with pytest.raises(DeliveryError, match="не найден"):
                await c.track_number("other")
            with pytest.raises(DeliveryError, match="пункт выдачи"):
                await c.create_shipment(cdek_shipment(point=None))

        # без пункта приёма — город отправителя; ошибки проверки — в requests[].errors
        invalid = {"requests": [{"state": "INVALID", "errors": [{"code": "v2", "message": "Телефон неверный"}]}]}
        handlers = cdek_handlers({("POST", "/v2/orders"): (400, invalid)})
        async with cdek_client(handlers) as (f, c):
            with pytest.raises(DeliveryError, match="Телефон неверный"):
                await c.create_shipment(cdek_shipment())
            assert f.paths("/v2/orders")[0]["body"]["from_location"] == {"code": 424}

    run(go())


def test_cdek_network_error():
    async def go():
        client = CdekClient("id", "secret", base_url="http://127.0.0.1:1/v2")
        try:
            with pytest.raises(DeliveryError, match="временно недоступна"):
                await client.quote(44, PARCEL)
        finally:
            await client.close()

    run(go())


# ---------- Почта России ----------

@contextlib.asynccontextmanager
async def pochta_client(handlers: dict):
    async with fake_server(handlers) as (fake, url):
        client = PochtaClient("app-token", "user@example.com", "pass", from_index="420000", base_url=url)
        try:
            yield fake, client
        finally:
            await client.close()


GOOD_ADDRESS = {"id": "1", "address-type": "DEFAULT", "original-address": "...", "index": "420111",
                "region": "Респ Татарстан", "place": "г Казань", "street": "ул Баумана", "house": "1",
                "corpus": "2", "room": "5", "quality-code": "GOOD", "validation-code": "VALIDATED"}


def test_pochta_headers_and_normalize():
    async def go():
        async with pochta_client({("POST", "/1.0/clean/address"): (200, [GOOD_ADDRESS])}) as (f, c):
            a = await c.normalize(" Казань, Баумана 1 к2, кв 5 ")
            assert a == PostAddress("420111", "Респ Татарстан", "г Казань", "ул Баумана", "1 к2", "5", "",
                                    "420111, Респ Татарстан, г Казань, ул Баумана, 1 к2, кв. 5")
            call = f.calls[-1]
            assert call["body"] == [{"id": "1", "original-address": "Казань, Баумана 1 к2, кв 5"}]
            h = call["headers"]
            assert h["Authorization"] == "AccessToken app-token"
            assert h["X-User-Authorization"] == "Basic " + base64.b64encode(b"user@example.com:pass").decode()
            assert h["Content-Type"] == "application/json;charset=UTF-8"

        # до востребования без квартиры — годится
        ok = {**GOOD_ADDRESS, "quality-code": "UNDEF_05", "validation-code": "CONFIRMED_MANUALLY", "room": None}
        async with pochta_client({("POST", "/1.0/clean/address"): (200, [ok])}) as (_, c):
            assert (await c.normalize("Казань")).room == ""

        # Почта возвращает room уже с «кв.» — не дублируем подпись в адресе и отправлении
        prefixed = {**GOOD_ADDRESS, "room": "кв. 5"}
        async with pochta_client({("POST", "/1.0/clean/address"): (200, [prefixed])}) as (_, c):
            a = await c.normalize("Казань, Баумана 1 к2, кв 5")
            assert a.room == "5"
            assert a.full.endswith("кв. 5") and "кв. кв." not in a.full

        bad = [{**GOOD_ADDRESS, "quality-code": "UNDEF_01"},
               {**GOOD_ADDRESS, "validation-code": "NOT_VALIDATED_HAS_NO_MAIN_POINTS"},
               {**GOOD_ADDRESS, "index": None}]
        for row in bad:
            async with pochta_client({("POST", "/1.0/clean/address"): (200, [row])}) as (_, c):
                with pytest.raises(DeliveryError, match="не распознала адрес"):
                    await c.normalize("абвгд")
        async with pochta_client({("POST", "/1.0/clean/address"): (200, [])}) as (f, c):
            with pytest.raises(DeliveryError, match="не распознала адрес"):
                await c.normalize("абвгд")
            with pytest.raises(DeliveryError, match="не распознала адрес"):
                await c.normalize("   ")
            assert len(f.calls) == 1  # пустой адрес не отправляем

    run(go())


def test_pochta_quote():
    ok = {"total-rate": 30658, "total-vat": 6132, "delivery-time": {"min-days": 1, "max-days": 3}}

    async def go():
        async with pochta_client({("POST", "/1.0/tariff"): (200, ok)}) as (f, c):
            q = await c.quote("420111", PARCEL)
            assert (q.cost, q.days_min, q.days_max) == (368, 1, 3)  # 367,90 ₽ → 368
            assert f.calls[-1]["body"] == {
                "index-from": "420000", "index-to": "420111", "mail-category": "ORDINARY",
                "mail-type": "ONLINE_PARCEL", "mass": 1200, "dimension": {"length": 30, "width": 20, "height": 10},
                "fragile": False}
            await c.quote("420111", PARCEL, declared_value=9980)
            body = f.calls[-1]["body"]
            assert body["mail-category"] == "WITH_DECLARED_VALUE" and body["declared-value"] == 998000

        async with pochta_client({("POST", "/1.0/tariff"): (200, {"total-rate": 10000})}) as (_, c):
            assert await c.quote("420111", PARCEL) == Quote(100)  # без НДС и срока
        bad_index = (400, {"code": "1001", "desc": "Неверный индекс"})
        async with pochta_client({("POST", "/1.0/tariff"): bad_index}) as (_, c):
            with pytest.raises(DeliveryError, match="не доставляет"):
                await c.quote("000000", PARCEL)
        for answer in ((401, {"message": "Unauthorized"}), (500, "oops"), (200, {"foo": 1})):
            async with pochta_client({("POST", "/1.0/tariff"): answer}) as (_, c):
                with pytest.raises(DeliveryError, match="временно недоступна"):
                    await c.quote("420111", PARCEL)

    run(go())


def pochta_shipment(**kw) -> Shipment:
    address = PostAddress("420111", "Респ Татарстан", "г Казань", "ул Баумана", "1", "", "", "...")
    return Shipment("ML-16", RECIPIENT, PARCEL, ITEMS, **{"declared_value": 9980, "post_address": address, **kw})


def test_pochta_create_and_track():
    async def go():
        handlers = {("PUT", "/1.0/user/backlog"): (200, {"result-ids": [115322331]}),
                    ("GET", "/1.0/backlog/115322331"): (200, {"id": 115322331, "barcode": "80082240994512"}),
                    ("GET", "/1.0/backlog/7"): (200, {"id": 7})}
        async with pochta_client(handlers) as (f, c):
            assert await c.create_shipment(pochta_shipment()) == "115322331"
            [order] = f.calls[-1]["body"]
            assert order == {
                "address-type-to": "DEFAULT", "index-to": 420111, "region-to": "Респ Татарстан",
                "place-to": "г Казань", "street-to": "ул Баумана", "house-to": "1",
                "surname": "Иванов", "given-name": "Иван", "middle-name": "Иванович",
                "recipient-name": "Иванов Иван Иванович", "tel-address": 79001234567,
                "mail-category": "WITH_DECLARED_VALUE", "mail-direct": 643, "mail-type": "ONLINE_PARCEL",
                "mass": 1200, "dimension": {"length": 30, "width": 20, "height": 10}, "fragile": False,
                "order-num": "ML-16", "postoffice-code": "420000", "insr-value": 998000}
            await c.create_shipment(pochta_shipment(declared_value=0))
            [order] = f.calls[-1]["body"]
            assert order["mail-category"] == "ORDINARY" and "insr-value" not in order

            assert await c.track_number("115322331") == "80082240994512"
            assert await c.track_number("7") is None
            with pytest.raises(DeliveryError, match="не найден"):
                await c.track_number("8")
            with pytest.raises(DeliveryError, match="адрес"):
                await c.create_shipment(pochta_shipment(post_address=None))

        errors = {"errors": [{"error-codes": [{"code": "EMPTY_INDEX_TO", "description": "Почтовый индекс не указан"}],
                              "position": 0}]}
        async with pochta_client({("PUT", "/1.0/user/backlog"): (200, errors)}) as (_, c):
            with pytest.raises(DeliveryError, match="Почтовый индекс не указан"):
                await c.create_shipment(pochta_shipment())

    run(go())


def test_pochta_network_error():
    async def go():
        client = PochtaClient("t", "l", "p", from_index="420000", base_url="http://127.0.0.1:1")
        try:
            with pytest.raises(DeliveryError, match="временно недоступна"):
                await client.normalize("Казань")
        finally:
            await client.close()

    run(go())
