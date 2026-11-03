"""СДЭК API v2: https://api-docs.cdek.ru (тестовая среда — https://api.edu.cdek.ru/v2)."""

import asyncio
import json
import logging
import math
import time
from typing import Any

import aiohttp

from app.delivery import City, DeliveryError, Parcel, Point, Quote, Shipment

log = logging.getLogger(__name__)

TIMEOUT = 15
TOKEN_MARGIN = 60  # обновляем токен заранее, чтобы он не истёк посреди запроса
POINTS_TTL = 6 * 3600  # ПВЗ меняются редко, а список по крупному городу — сотни записей
UNAVAILABLE = "Доставка СДЭК временно недоступна, попробуйте позже"
NO_ROUTE = "СДЭК не доставляет по этому направлению"


class CdekClient:
    def __init__(self, client_id: str, client_secret: str, *, base_url: str = "https://api.cdek.ru/v2",
                 from_city_code: int = 424, tariff_code: int = 136, shipment_point: str = "") -> None:
        self.client_id = client_id
        self._secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.from_city_code = from_city_code
        self.tariff_code = tariff_code
        self.shipment_point = shipment_point
        self._http: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._token_until = 0.0
        self._token_lock = asyncio.Lock()
        self._points: dict[int, tuple[float, list[Point]]] = {}

    async def cities(self, query: str, limit: int = 10) -> list[City]:
        query = query.strip()
        if not query:
            return []
        # suggest/cities ищет по началу названия (для подсказок), а location/cities — только точное совпадение
        data = await self._get("/location/suggest/cities", {"name": query, "country_code": "RU"})
        cities = []
        for row in data if isinstance(data, list) else []:
            if not isinstance(row, dict) or not isinstance(row.get("code"), int):
                continue
            # full_name: «Казань, Татарстан респ., Россия» — город, регион(ы), страна
            parts = [p.strip() for p in str(row.get("full_name") or row.get("city") or "").split(",") if p.strip()]
            if not parts:
                continue
            region = ", ".join(parts[1:-1] if len(parts) > 2 else parts[1:])
            cities.append(City(row["code"], parts[0], region))
            if len(cities) >= limit:
                break
        return cities

    async def points(self, city_code: int) -> list[Point]:
        cached = self._points.get(city_code)
        if cached and time.monotonic() - cached[0] < POINTS_TTL:
            return cached[1]
        # Только PVZ: тариф 136 «склад-склад» — до пункта выдачи; в постамат возит отдельный тариф 368
        # «склад-постамат», и заказ на 136 с постаматом в delivery_point СДЭК не примет.
        # is_handout — только пункты, которые выдают посылки (бывают пункты только для приёма).
        data = await self._get("/deliverypoints", {"city_code": city_code, "type": "PVZ", "is_handout": "true"})
        points = []
        for row in data if isinstance(data, list) else []:
            if not isinstance(row, dict) or not row.get("code"):
                continue
            loc = row.get("location") if isinstance(row.get("location"), dict) else {}
            points.append(Point(
                code=str(row["code"]),
                name=str(row.get("name") or ""),
                # address_full — с городом: этот адрес попадает в заказ, сообщения покупателю и админу
                address=str(loc.get("address_full") or loc.get("address") or ""),
                work_time=str(row.get("work_time") or ""),
                latitude=_float(loc.get("latitude")),
                longitude=_float(loc.get("longitude")),
            ))
        self._points[city_code] = (time.monotonic(), points)
        return points

    async def point(self, city_code: int, code: str) -> Point | None:
        return next((p for p in await self.points(city_code) if p.code == code), None)

    async def quote(self, city_code: int, parcel: Parcel) -> Quote:
        body = {
            "type": 1,  # интернет-магазин: тарифы «Посылка» доступны только ему
            "tariff_code": self.tariff_code,
            "from_location": {"code": self.from_city_code},
            "to_location": {"code": city_code},
            "packages": [_package(parcel)],
        }
        try:
            data = await self._request("POST", "/calculator/tariff", json=body)
        except _ApiError as e:
            # калькулятор отвечает 400 с errors[], если тариф недоступен для направления или веса
            raise DeliveryError(NO_ROUTE) from e
        if not isinstance(data, dict) or data.get("errors"):
            log.warning("СДЭК калькулятор: %s", _messages(data) or data)
            raise DeliveryError(NO_ROUTE)
        total = data.get("total_sum", data.get("delivery_sum"))
        if not isinstance(total, int | float):
            log.warning("СДЭК калькулятор: нет суммы в ответе %s", data)
            raise DeliveryError(UNAVAILABLE)
        return Quote(math.ceil(total), _int(data.get("period_min")), _int(data.get("period_max")))

    async def create_shipment(self, shipment: Shipment) -> str:
        if not shipment.cdek_point:
            raise DeliveryError("Не выбран пункт выдачи СДЭК")
        r = shipment.recipient
        recipient: dict[str, Any] = {
            "name": " ".join(p for p in (r.surname, r.name, r.patronymic) if p),
            "phones": [{"number": r.phone}],
        }
        if r.email:
            recipient["email"] = r.email
        package = _package(shipment.parcel)
        package["number"] = "1"
        package["items"] = [{
            "name": f"{it.title}, размер {it.size}" if it.size else it.title,
            "ware_key": f"{it.title} {it.size}".strip()[:50],
            "payment": {"value": 0},  # 100% предоплата на сайте — с получателя ничего не берём
            "cost": it.price,
            "weight": it.weight_g,
            "amount": it.qty,
        } for it in shipment.items]
        body: dict[str, Any] = {
            "type": 1,
            "number": shipment.order_number,
            "tariff_code": self.tariff_code,
            "delivery_point": shipment.cdek_point,
            "recipient": recipient,
            "packages": [package],
        }
        # «склад-*»: продавец сам сдаёт посылку в ПВЗ отправки; без shipment_point СДЭК берёт город отправителя
        if self.shipment_point:
            body["shipment_point"] = self.shipment_point
        else:
            body["from_location"] = {"code": self.from_city_code}
        try:
            data = await self._request("POST", "/orders", json=body)
        except _ApiError as e:
            raise DeliveryError(f"СДЭК не принял заказ: {e.text or 'ошибка сервиса'}") from e
        uuid = (data.get("entity") or {}).get("uuid") if isinstance(data, dict) else None
        if _invalid(data) or not uuid:
            log.warning("СДЭК заказ %s: %s", shipment.order_number, _messages(data) or data)
            raise DeliveryError(f"СДЭК не принял заказ: {_messages(data) or 'ошибка сервиса'}")
        return str(uuid)

    async def track_number(self, shipment_id: str) -> str | None:
        """Номер накладной СДЭК. Заказ регистрируется асинхронно: сразу после создания номера ещё нет."""
        try:
            data = await self._request("GET", f"/orders/{shipment_id}")
        except _ApiError as e:
            raise DeliveryError(f"СДЭК: заказ не найден ({e.text or 'ошибка сервиса'})") from e
        if _invalid(data):
            # проверка заказа идёт уже после ответа на POST /orders — ошибки видны только здесь
            raise DeliveryError(f"СДЭК отклонил заказ: {_messages(data) or 'ошибка проверки'}")
        number = (data.get("entity") or {}).get("cdek_number") if isinstance(data, dict) else None
        return str(number) if number else None

    async def close(self) -> None:
        if self._http:
            await self._http.close()
            self._http = None

    async def _access_token(self) -> str:
        async with self._token_lock:  # параллельные запросы не должны получать токен каждый сам
            if self._token and time.monotonic() < self._token_until:
                return self._token
            form = {"grant_type": "client_credentials", "client_id": self.client_id,
                    "client_secret": self._secret}
            status, data = await self._send("POST", "/oauth/token", data=form)
            token = data.get("access_token") if isinstance(data, dict) else None
            if status != 200 or not token:
                # тело ответа на /oauth/token секретов не содержит, а ключи в лог не пишем
                log.warning("СДЭК авторизация: HTTP %s %s", status, _messages(data) or data)
                raise DeliveryError(UNAVAILABLE)
            expires = _int(data.get("expires_in")) or 3600
            self._token = token
            self._token_until = time.monotonic() + max(expires - TOKEN_MARGIN, 0)
            return token

    async def _get(self, path: str, params: dict) -> Any:
        try:
            return await self._request("GET", path, params=params)
        except _ApiError as e:
            raise DeliveryError(UNAVAILABLE) from e

    async def _request(self, method: str, path: str, *, retry: bool = True, **kwargs) -> Any:
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        status, data = await self._send(method, path, headers=headers, **kwargs)
        if status == 401 and retry:
            # токен могли отозвать раньше срока — один раз берём новый
            self._token = None
            return await self._request(method, path, retry=False, **kwargs)
        if not 200 <= status < 300:
            text = _messages(data)
            log.warning("СДЭК %s %s: HTTP %s %s", method, path, status, text or data)
            if status >= 500 or status == 401:
                raise DeliveryError(UNAVAILABLE)
            raise _ApiError(status, text)
        return data

    async def _send(self, method: str, path: str, **kwargs) -> tuple[int, Any]:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT))
        try:
            async with self._http.request(method, self.base_url + path, **kwargs) as resp:
                text = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("СДЭК %s %s: %r", method, path, e)
            raise DeliveryError(UNAVAILABLE) from e
        try:
            return status, json.loads(text) if text else None
        except ValueError as e:
            log.warning("СДЭК %s %s: HTTP %s, не JSON: %.200s", method, path, status, text)
            raise DeliveryError(UNAVAILABLE) from e


class _ApiError(DeliveryError):
    """4xx от СДЭК: вызывающий метод решает, какой текст показать покупателю."""

    def __init__(self, status: int, text: str):
        super().__init__(text or UNAVAILABLE)
        self.status = status
        self.text = text


def _package(parcel: Parcel) -> dict[str, Any]:
    return {"weight": parcel.weight_g, "length": parcel.length_cm,
            "width": parcel.width_cm, "height": parcel.height_cm}


def _invalid(data: Any) -> bool:
    requests = data.get("requests") if isinstance(data, dict) else None
    return any(isinstance(r, dict) and r.get("state") == "INVALID" for r in requests or [])


def _messages(data: Any) -> str:
    """Тексты ошибок: errors[] в корне (калькулятор, 4xx) и requests[].errors (заказы)."""
    if not isinstance(data, dict):
        return ""
    errors = list(data.get("errors") or [])
    for r in data.get("requests") or []:
        if isinstance(r, dict):
            errors += r.get("errors") or []
    return "; ".join(str(e.get("message") or e.get("code")) for e in errors if isinstance(e, dict))


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
