"""Почта России, API «Отправка»: https://otpravka.pochta.ru/specification (тестовой среды нет)."""

import base64
import json
import logging
import math
import re
from typing import Any

import aiohttp

from app.delivery import DeliveryError, Parcel, PostAddress, Quote, Shipment

log = logging.getLogger(__name__)

TIMEOUT = 15
UNAVAILABLE = "Доставка Почтой России временно недоступна, попробуйте позже"
BAD_ADDRESS = "Почта не распознала адрес. Укажите город, улицу, дом и индекс"
# Справочники «Код качества нормализации адреса» и «Код проверки нормализации адреса»:
# по этим значениям Почта считает адрес пригодным для отправки. ON_DEMAND — «до востребования»,
# POSTAL_BOX — абонентский ящик, UNDEF_05 — не указана квартира: посылку всё равно выдают в отделении.
GOOD_QUALITY = frozenset({"GOOD", "POSTAL_BOX", "ON_DEMAND", "UNDEF_05"})
GOOD_VALIDATION = frozenset({"VALIDATED", "OVERRIDDEN", "CONFIRMED_MANUALLY"})
# Почта иногда отдаёт room уже с подписью («кв. 5»), а в заказе и API отправления нужен только номер
ROOM_PREFIX = re.compile(r"^(?:кварт(?:ира)?|кв)\.?\s*", re.IGNORECASE)
RUSSIA = 643  # mail-direct: код страны назначения
# «Посылка онлайн» — тариф для интернет-магазинов по договору, дешевле обычной «Посылки» (POSTAL_PARCEL);
# выдаётся в отделении по индексу получателя
MAIL_TYPE = "ONLINE_PARCEL"


class PochtaClient:
    def __init__(self, token: str, login: str, password: str, *, from_index: str,
                 base_url: str = "https://otpravka-api.pochta.ru") -> None:
        self.from_index = from_index
        self.base_url = base_url.rstrip("/")
        basic = base64.b64encode(f"{login}:{password}".encode()).decode()
        self._headers = {
            "Authorization": f"AccessToken {token}",
            "X-User-Authorization": f"Basic {basic}",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json;charset=UTF-8",
        }
        self._http: aiohttp.ClientSession | None = None

    async def normalize(self, address: str) -> PostAddress:
        address = address.strip()
        if not address:
            raise DeliveryError(BAD_ADDRESS)
        data = await self._request("POST", "/1.0/clean/address", [{"id": "1", "original-address": address}])
        row = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        index = str(row.get("index") or "")
        if (row.get("quality-code") not in GOOD_QUALITY or row.get("validation-code") not in GOOD_VALIDATION
                or not re.fullmatch(r"\d{6}", index) or not row.get("place")):
            log.info("Почта: адрес не годится: quality=%s validation=%s",
                     row.get("quality-code"), row.get("validation-code"))
            raise DeliveryError(BAD_ADDRESS)
        house = _s(row, "house")
        # корпус/строение/литера/дробь Почта отдаёт отдельно, а у нас одно поле «дом»
        for key, prefix in (("corpus", " к"), ("building", " стр"), ("letter", ""), ("slash", "/")):
            if row.get(key):
                house += prefix + _s(row, key)
        addr = PostAddress(index=index, region=_s(row, "region"), place=_s(row, "place"), street=_s(row, "street"),
                           house=house.strip(), room=_room(_s(row, "room")), area=_s(row, "area"))
        full = ", ".join(p for p in (addr.index, addr.region, addr.area, addr.place, addr.street, addr.house,
                                     f"кв. {addr.room}" if addr.room else "") if p)
        return PostAddress(addr.index, addr.region, addr.place, addr.street, addr.house, addr.room, addr.area, full)

    async def quote(self, index_to: str, parcel: Parcel, declared_value: int = 0) -> Quote:
        body: dict[str, Any] = {
            "index-from": self.from_index,
            "index-to": index_to,
            "mail-category": _category(declared_value),
            "mail-type": MAIL_TYPE,
            "mass": parcel.weight_g,
            "dimension": _dimension(parcel),
            "fragile": False,
        }
        if declared_value > 0:
            body["declared-value"] = declared_value * 100
        try:
            data = await self._request("POST", "/1.0/tariff", body)
        except _ApiError as e:
            raise DeliveryError("Почта России не доставляет по этому адресу") from e
        rate, vat = (data.get("total-rate"), data.get("total-vat", 0)) if isinstance(data, dict) else (None, None)
        if not isinstance(rate, int | float) or not isinstance(vat, int | float):
            log.warning("Почта тариф: нет суммы в ответе %s", data)
            raise DeliveryError(UNAVAILABLE)
        days = data.get("delivery-time") if isinstance(data.get("delivery-time"), dict) else {}
        # суммы — в копейках, покупателю показываем целые рубли с округлением вверх
        return Quote(math.ceil((rate + vat) / 100), _int(days.get("min-days")), _int(days.get("max-days")))

    async def create_shipment(self, shipment: Shipment) -> str:
        a = shipment.post_address
        if a is None:
            raise DeliveryError("Не указан адрес доставки Почтой России")
        r = shipment.recipient
        phone = re.sub(r"\D", "", r.phone)
        order: dict[str, Any] = {
            "address-type-to": "DEFAULT",
            "index-to": int(a.index),
            "region-to": a.region,
            "area-to": a.area,
            "place-to": a.place,
            "street-to": a.street,
            "house-to": a.house,
            "room-to": a.room,
            "surname": r.surname,
            "given-name": r.name,
            "middle-name": r.patronymic,
            "recipient-name": " ".join(p for p in (r.surname, r.name, r.patronymic) if p),
            "tel-address": int(phone) if phone else None,  # API ждёт число: 79001234567
            "mail-category": _category(shipment.declared_value),
            "mail-direct": RUSSIA,
            "mail-type": MAIL_TYPE,
            "mass": shipment.parcel.weight_g,
            "dimension": _dimension(shipment.parcel),
            "fragile": False,
            "order-num": shipment.order_number,
            "postoffice-code": self.from_index,
        }
        if shipment.declared_value > 0:
            order["insr-value"] = shipment.declared_value * 100
        order = {k: v for k, v in order.items() if v not in ("", None)}
        try:
            data = await self._request("PUT", "/1.0/user/backlog", [order])
        except _ApiError as e:
            raise DeliveryError(f"Почта не приняла заказ: {e.text or 'ошибка сервиса'}") from e
        ids = data.get("result-ids") if isinstance(data, dict) else None
        if not ids:
            # ошибки валидации приходят с HTTP 200: {"errors": [{"error-codes": [...], "position": 0}]}
            text = _messages(data)
            log.warning("Почта заказ %s: %s", shipment.order_number, text or data)
            raise DeliveryError(f"Почта не приняла заказ: {text or 'ошибка сервиса'}")
        return str(ids[0])

    async def track_number(self, shipment_id: str) -> str | None:
        try:
            data = await self._request("GET", f"/1.0/backlog/{shipment_id}")
        except _ApiError as e:
            raise DeliveryError("Почта: заказ не найден") from e
        barcode = data.get("barcode") if isinstance(data, dict) else None
        return str(barcode) if barcode else None

    async def close(self) -> None:
        if self._http:
            await self._http.close()
            self._http = None

    async def _request(self, method: str, path: str, body: Any = None) -> Any:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT))
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        try:
            async with self._http.request(method, self.base_url + path, data=payload, headers=self._headers) as resp:
                status, text = resp.status, await resp.text()
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("Почта %s %s: %r", method, path, e)
            raise DeliveryError(UNAVAILABLE) from e
        try:
            data = json.loads(text) if text else None
        except ValueError as e:
            log.warning("Почта %s %s: HTTP %s, не JSON: %.200s", method, path, status, text)
            raise DeliveryError(UNAVAILABLE) from e
        if not 200 <= status < 300:
            msg = _messages(data)
            log.warning("Почта %s %s: HTTP %s %s", method, path, status, msg or data)
            # 401/403 — неверный токен или логин/пароль: покупателю это ни к чему
            if status >= 500 or status in (401, 403):
                raise DeliveryError(UNAVAILABLE)
            raise _ApiError(status, msg)
        return data


class _ApiError(DeliveryError):
    """4xx от Почты: вызывающий метод решает, какой текст показать покупателю."""

    def __init__(self, status: int, text: str):
        super().__init__(text or UNAVAILABLE)
        self.status = status
        self.text = text


def _category(declared_value: int) -> str:
    return "WITH_DECLARED_VALUE" if declared_value > 0 else "ORDINARY"


def _dimension(parcel: Parcel) -> dict[str, int]:
    return {"length": parcel.length_cm, "width": parcel.width_cm, "height": parcel.height_cm}


def _messages(data: Any) -> str:
    """Ошибки бывают {"desc"/"message": ...} и {"errors": [{"error-codes": [{"description"}]}]}."""
    if not isinstance(data, dict):
        return ""
    texts = []
    for err in data.get("errors") or []:
        for code in (err.get("error-codes") or []) if isinstance(err, dict) else []:
            if isinstance(code, dict):
                texts.append(str(code.get("description") or code.get("code")))
    if not texts:
        texts = [str(data[k]) for k in ("desc", "message") if data.get(k)][:1]
    return "; ".join(texts)


def _s(row: dict, key: str) -> str:
    return str(row.get(key) or "").strip()


def _room(value: str) -> str:
    return ROOM_PREFIX.sub("", value).strip()


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
