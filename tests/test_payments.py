import asyncio
import base64
import contextlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.payments import PaymentError, ReceiptLine, YooKassa, kopecks
from tests.conftest import run

SHOP, SECRET = "123456", "test_SecretKey"
CONFIRM = "https://yoomoney.ru/checkout/payments/v2/contract?orderId=2d"


class FakeApi:
    """Фейковая ЮKassa: проверяет Basic-авторизацию и отвечает заранее заданным (status, body)."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict | None, dict]] = []  # method, path, body, headers
        self.reply: tuple[int, object] | None = None
        self.delay = 0.0

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.method, request.match_info["path"], body, dict(request.headers)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.reply is not None:
            status, payload = self.reply
            if isinstance(payload, str):
                return web.Response(status=status, text=payload)
            return web.json_response(payload, status=status)
        auth = "Basic " + base64.b64encode(f"{SHOP}:{SECRET}".encode()).decode()
        if request.headers.get("Authorization") != auth:
            return web.json_response({"type": "error", "code": "invalid_credentials",
                                      "description": "Неверный shopId или секретный ключ"}, status=401)
        path = request.match_info["path"]
        if path == "payments":
            return web.json_response({"id": "2d", "status": "pending", "paid": False, "amount": body["amount"],
                                      "confirmation": {"type": "redirect", "confirmation_url": CONFIRM},
                                      "metadata": body["metadata"]})
        if path.startswith("payments/"):
            return web.json_response({"id": path.split("/")[1], "status": "succeeded", "paid": True,
                                      "amount": {"value": "3290.00", "currency": "RUB"},
                                      "metadata": {"order_id": "7"}})
        if path == "refunds":
            return web.json_response({"id": "r1", "status": "succeeded", "payment_id": body["payment_id"],
                                      "amount": body["amount"]})
        return web.json_response({"id": path.split("/")[1], "status": "succeeded", "payment_id": "2d"})


@contextlib.asynccontextmanager
async def api_client(**kwargs):
    api = FakeApi()
    app = web.Application()
    app.router.add_route("*", "/v3/{path:.+}", api.handle)
    async with TestServer(app) as server:
        kassa = YooKassa(SHOP, SECRET, base_url=str(server.make_url("/v3")), **kwargs)
        try:
            yield kassa, api
        finally:
            await kassa.close()


async def create(kassa: YooKassa, **kwargs):
    args = dict(order_id=7, amount=3290, description="Заказ #7", return_url="https://s.test/pay/7/x?return=1",
                email="a@b.ru", phone="+79001234567")
    return await kassa.create(**(args | kwargs))


def test_kopecks():
    assert kopecks({"value": "5530.00", "currency": "RUB"}) == 553000
    assert kopecks({"value": "10.5", "currency": "RUB"}) == 1050
    for bad in ({"value": "1.00", "currency": "USD"}, {"value": "abc", "currency": "RUB"},
                {"value": "1.001", "currency": "RUB"}, {"value": "NaN", "currency": "RUB"}, None, "1.00"):
        assert kopecks(bad) is None, bad


def test_create_payment():
    async def go():
        async with api_client() as (kassa, api):
            payment = await create(kassa, receipt=[ReceiptLine("Худи", 3290, 1)])
            assert (payment.id, payment.status, payment.url, payment.amount, payment.order_id) == (
                "2d", "pending", CONFIRM, 329000, 7)
            method, path, body, headers = api.requests[0]
            assert (method, path) == ("POST", "payments") and headers["Idempotence-Key"]
            assert body == {"amount": {"value": "3290.00", "currency": "RUB"}, "capture": True,
                            "confirmation": {"type": "redirect", "return_url": "https://s.test/pay/7/x?return=1"},
                            "description": "Заказ #7", "metadata": {"order_id": "7"}}  # без НДС — чека нет
            await create(kassa, idempotence_key="7-after-1", description="Ж" * 200)
            assert api.requests[1][3]["Idempotence-Key"] == "7-after-1"
            assert api.requests[0][3]["Idempotence-Key"] != api.requests[1][3]["Idempotence-Key"]
            assert len(api.requests[1][2]["description"]) == 128
    run(go())


def test_receipt_only_with_vat_code():
    async def go():
        async with api_client(vat_code=1) as (kassa, api):
            await create(kassa, amount=7000, receipt=[ReceiptLine("Ф" * 200, 3000, 2),
                                                      ReceiptLine("Доставка СДЭК", 1000, 1, "service")])
            receipt = api.requests[0][2]["receipt"]
            assert receipt["customer"] == {"email": "a@b.ru", "phone": "79001234567"}
            first, delivery = receipt["items"]
            assert len(first["description"]) == 128
            assert (first["quantity"], first["amount"], first["vat_code"]) == (
                "2", {"value": "3000.00", "currency": "RUB"}, 1)
            assert first["payment_mode"] == "full_prepayment"
            assert first["payment_subject"] == "commodity" and delivery["payment_subject"] == "service"

            await create(kassa, receipt=None)
            assert "receipt" not in api.requests[1][2]
            with pytest.raises(PaymentError):  # сумма чека не сходится с суммой платежа
                await create(kassa, amount=100, receipt=[ReceiptLine("Худи", 3290, 1)])
            assert len(api.requests) == 2
    run(go())


def test_get_and_refund():
    async def go():
        async with api_client() as (kassa, api):
            payment = await kassa.get("2d")
            assert (payment.status, payment.amount, payment.url, payment.order_id) == ("succeeded", 329000, None, 7)
            refund = await kassa.refund("2d", 3290)
            assert (refund.id, refund.status, refund.payment_id) == ("r1", "succeeded", "2d")
            assert (await kassa.get_refund("r1")).payment_id == "2d"
            (m1, p1, _, _), (m2, p2, body, headers), (m3, p3, _, _) = api.requests
            assert (m1, p1, m2, p2, m3, p3) == ("GET", "payments/2d", "POST", "refunds", "GET", "refunds/r1")
            assert body == {"payment_id": "2d", "amount": {"value": "3290.00", "currency": "RUB"}}
            assert headers["Idempotence-Key"] == "refund-2d"  # повторный клик не вернёт деньги дважды
    run(go())


def test_wrong_credentials():
    async def go():
        async with api_client() as (kassa, _):
            kassa._secret = "wrong"
            with pytest.raises(PaymentError, match="Неверный shopId"):
                await kassa.get("2d")
    run(go())


@pytest.mark.parametrize("reply", [
    (400, {"type": "error", "code": "invalid_request", "description": "Неверные параметры"}),
    (500, {"type": "error", "code": "internal_server_error"}),
    (200, "<html>не JSON</html>"),
    (200, ["id"]),
    (200, {"status": "pending"}),  # нет id
    (200, {"id": "2d", "status": "pending", "confirmation": {"type": "redirect"}}),  # нет ссылки на оплату
])
def test_bad_replies_raise(reply):
    async def go():
        async with api_client() as (kassa, api):
            api.reply = reply
            with pytest.raises(PaymentError):
                await create(kassa)
            if reply[1] != {"id": "2d", "status": "pending", "confirmation": {"type": "redirect"}}:
                for call in (kassa.get("1"), kassa.refund("1", 1), kassa.get_refund("1")):
                    with pytest.raises(PaymentError):
                        await call
    run(go())


def test_timeout_and_network_errors(monkeypatch):
    import aiohttp

    from app import payments

    monkeypatch.setattr(payments, "TIMEOUT", aiohttp.ClientTimeout(total=0.1))

    async def go():
        async with api_client() as (kassa, api):
            api.delay = 0.5
            with pytest.raises(PaymentError, match="недоступна"):
                await kassa.get("1")
        kassa = YooKassa(SHOP, SECRET, base_url="http://127.0.0.1:1/v3")
        try:
            with pytest.raises(PaymentError, match="недоступна"):
                await kassa.get("1")
        finally:
            await kassa.close()
    run(go())


def test_session_lazy_and_secret_hidden(caplog):
    async def go():
        kassa = YooKassa(SHOP, SECRET)
        assert kassa._http is None
        await kassa.close()  # закрытие без запросов не падает
        async with api_client() as (kassa, api):
            api.reply = (401, {"type": "error", "code": "invalid_credentials", "description": "Ошибка"})
            with pytest.raises(PaymentError):
                await create(kassa)
            return repr(kassa)
    with caplog.at_level("DEBUG", logger="app.payments"):
        text = run(go())
    assert SECRET not in caplog.text and SECRET not in text
