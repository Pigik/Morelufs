"""ЮKassa, API v3: создание платежа, статус, возврат.

Документация: https://yookassa.ru/developers/api (платежи, возвраты), .../using-api/webhooks (уведомления).
Авторизация — HTTP Basic: shopId и секретный ключ. Тестовый магазин работает на том же адресе, что и боевой.
Уведомления ЮKassa не подписывает: их подлинность проверяем, запрашивая объект заново по API.
"""

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://api.yookassa.ru/v3"
TIMEOUT = aiohttp.ClientTimeout(total=15)
DESCRIPTION_MAX = 128  # лимит description платежа и позиции чека


class PaymentError(Exception):
    """Платёж не создан/не получен. Текст — для логов и админа, покупателю показываем общий."""


@dataclass(frozen=True, slots=True)
class Payment:
    id: str
    status: str  # pending | waiting_for_capture | succeeded | canceled
    amount: int | None  # копейки; None — ЮKassa не прислала сумму
    url: str | None  # страница оплаты, пока платёж pending
    order_id: int | None = None  # из metadata: к какому нашему заказу относится платёж
    cancel_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Refund:
    id: str
    status: str  # pending | succeeded | canceled
    payment_id: str


@dataclass(frozen=True, slots=True)
class ReceiptLine:
    name: str
    price: int  # рубли за штуку
    qty: int
    payment_subject: str = "commodity"  # доставка — "service"


def rub(amount: int) -> dict:
    return {"value": f"{amount}.00", "currency": "RUB"}


def kopecks(amount) -> int | None:
    """{"value": "123.45", "currency": "RUB"} → 12345; чужая валюта или мусор — None."""
    if not isinstance(amount, dict) or amount.get("currency") != "RUB":
        return None
    try:
        value = Decimal(str(amount.get("value")))
    except InvalidOperation:
        return None
    return int(value * 100) if value.is_finite() and value == value.quantize(Decimal("0.01")) else None


class YooKassa:
    def __init__(self, shop_id: str, secret_key: str, *, base_url: str = API_URL, vat_code: int | None = None) -> None:
        self.shop_id = shop_id
        self._secret = secret_key
        self.base_url = base_url.rstrip("/")
        self.vat_code = vat_code  # None — чек через ЮKassa не формируем (самозанятый пробивает в «Мой налог»)
        self._http: aiohttp.ClientSession | None = None

    def __repr__(self) -> str:  # секретный ключ не должен попасть в логи через repr
        return f"YooKassa(shop_id={self.shop_id!r}, base_url={self.base_url!r})"

    async def create(self, *, order_id: int, amount: int, description: str, return_url: str,
                     email: str = "", phone: str = "", receipt: list[ReceiptLine] | None = None,
                     idempotence_key: str | None = None) -> Payment:
        body: dict = {
            "amount": rub(amount),
            "capture": True,  # одностадийная оплата: деньги списываются сразу
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:DESCRIPTION_MAX],
            "metadata": {"order_id": str(order_id)},
        }
        if receipt is not None and self.vat_code is not None:
            body["receipt"] = self._receipt(receipt, email, phone, amount)
        payment = self._payment(await self._call("POST", "payments", body, idempotence_key or str(uuid.uuid4())))
        if not payment.url:
            log.warning("ЮKassa: нет confirmation_url у платежа %s (заказ #%s)", payment.id, order_id)
            raise PaymentError("ЮKassa не вернула ссылку на оплату")
        return payment

    def _receipt(self, lines: list[ReceiptLine], email: str, phone: str, amount: int) -> dict:
        items = [{
            "description": line.name[:DESCRIPTION_MAX],
            "quantity": str(line.qty),
            "amount": rub(line.price),
            "vat_code": self.vat_code,
            "payment_mode": "full_prepayment",
            "payment_subject": line.payment_subject,
        } for line in lines]
        # ЮKassa отклонит чек, сумма которого не равна сумме платежа; ловим раньше с понятным текстом
        if sum(line.price * line.qty for line in lines) != amount:
            raise PaymentError("сумма позиций чека не равна сумме платежа")
        customer = {k: v for k, v in (("email", email), ("phone", phone.lstrip("+"))) if v}
        return {"customer": customer, "items": items}

    async def get(self, payment_id: str) -> Payment:
        return self._payment(await self._call("GET", f"payments/{payment_id}"))

    async def refund(self, payment_id: str, amount: int) -> Refund:
        # ключ от платежа: повторное нажатие «Вернуть деньги» не создаст второй возврат
        data = await self._call("POST", "refunds", {"payment_id": payment_id, "amount": rub(amount)},
                                f"refund-{payment_id}")
        return self._refund(data)

    async def get_refund(self, refund_id: str) -> Refund:
        return self._refund(await self._call("GET", f"refunds/{refund_id}"))

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None

    @staticmethod
    def _payment(data: dict) -> Payment:
        pid, status = data.get("id"), data.get("status")
        if not isinstance(pid, str) or not pid or not isinstance(status, str) or not status:
            log.warning("ЮKassa: в ответе нет id/status платежа")
            raise PaymentError("ЮKassa вернула непонятный ответ")
        confirmation = data.get("confirmation") if isinstance(data.get("confirmation"), dict) else {}
        url = confirmation.get("confirmation_url")
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        order_id = str(metadata.get("order_id", ""))
        details = data.get("cancellation_details") if isinstance(data.get("cancellation_details"), dict) else {}
        return Payment(pid, status, kopecks(data.get("amount")), url if isinstance(url, str) and url else None,
                       int(order_id) if order_id.isdigit() else None, details.get("reason"))

    @staticmethod
    def _refund(data: dict) -> Refund:
        rid, status, pid = data.get("id"), data.get("status"), data.get("payment_id")
        if not all(isinstance(v, str) and v for v in (rid, status, pid)):
            log.warning("ЮKassa: в ответе нет id/status возврата")
            raise PaymentError("ЮKassa вернула непонятный ответ")
        return Refund(rid, status, pid)

    async def _call(self, method: str, path: str, body: dict | None = None, idempotence_key: str | None = None) -> dict:
        if self._http is None:  # в __init__ ещё нет event loop'а
            self._http = aiohttp.ClientSession(timeout=TIMEOUT, auth=aiohttp.BasicAuth(self.shop_id, self._secret))
        headers = {"Idempotence-Key": idempotence_key} if idempotence_key else {}
        status = 0
        try:
            async with self._http.request(method, f"{self.base_url}/{path}", json=body, headers=headers) as resp:
                status = resp.status
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("ЮKassa %s %s: сеть: %r", method, path, e)
            raise PaymentError("ЮKassa недоступна") from e
        except ValueError as e:  # битый JSON
            log.warning("ЮKassa %s %s: HTTP %s, не JSON в ответе", method, path, status)
            raise PaymentError("ЮKassa вернула непонятный ответ") from e
        if not isinstance(data, dict):
            log.warning("ЮKassa %s %s: ответ не объект", method, path)
            raise PaymentError("ЮKassa вернула непонятный ответ")
        if status >= 300 or data.get("type") == "error":
            code, text = data.get("code"), data.get("description")
            log.warning("ЮKassa %s %s: HTTP %s, %s: %s (параметр %s)", method, path, status, code, text,
                        data.get("parameter"))
            raise PaymentError(f"ЮKassa: {text or 'ошибка'} ({code or status})")
        return data
