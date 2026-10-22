"""Вход на сайт через Telegram (OpenID Connect, Authorization Code + PKCE).

https://core.telegram.org/bots/telegram-login
Мини-апп этим не пользуется: там пользователь уже известен по подписи initData (app/auth.py).
"""

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import aiohttp
import jwt

ISSUER = "https://oauth.telegram.org"
AUTH_URL = f"{ISSUER}/auth"
TOKEN_URL = f"{ISSUER}/token"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
# profile — id, имя, ник; phone — подтверждённый номер; bot_access — бот сможет писать о заказах
SCOPE = "openid profile phone telegram:bot_access"
ALGORITHMS = ["RS256", "ES256"]  # RS256 по умолчанию; ES256 можно включить в @BotFather
JWKS_TTL = 3600


class LoginError(Exception):
    """Вход не удался; текст — для логов, покупатель видит общее сообщение."""


def new_pkce() -> tuple[str, str]:
    """(verifier, challenge) для PKCE S256."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def verify_id_token(token: str, jwks: dict, client_id: str, nonce: str, leeway: int = 30) -> dict:
    """Проверяет подпись и поля id_token. Возвращает claims или бросает LoginError."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        keys = [k for k in jwt.PyJWKSet.from_dict(jwks).keys if kid is None or k.key_id == kid]
        if not keys:
            raise LoginError(f"нет ключа {kid!r} в JWKS")
        claims = jwt.decode(
            token, keys[0], algorithms=ALGORITHMS, audience=client_id, issuer=ISSUER, leeway=leeway,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.PyJWTError as e:
        raise LoginError(f"id_token не прошёл проверку: {e}") from e
    # id_token приходит напрямую с /token по TLS (code + PKCE), поэтому nonce — доп. защита:
    # если Telegram его вернул, он обязан совпасть
    if "nonce" in claims and not secrets.compare_digest(str(claims["nonce"]).encode(), nonce.encode()):
        raise LoginError("nonce не совпал")
    try:
        claims["id"] = int(claims["id"])  # Telegram user id — тот же, что в мини-аппе и у бота
    except (KeyError, TypeError, ValueError) as e:
        raise LoginError("в id_token нет Telegram id (нужен scope profile)") from e
    return claims


def phone_from_claims(claims: dict) -> str | None:
    """Подтверждённый российский номер в формате формы заказа: (900) 123-45-67."""
    if claims.get("phone_number_verified") is False:
        return None
    digits = "".join(ch for ch in str(claims.get("phone_number") or "") if ch.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        d = digits[1:]
        return f"({d[:3]}) {d[3:6]}-{d[6:8]}-{d[8:]}"
    return None  # иностранный номер: в форму не подставляем, у неё маска +7


class TelegramLogin:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._http: aiohttp.ClientSession | None = None
        self._jwks: dict | None = None
        self._jwks_at = 0.0

    def authorize_url(self, state: str, challenge: str, nonce: str) -> str:
        return f"{AUTH_URL}?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": SCOPE, "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })

    async def exchange(self, code: str, verifier: str, nonce: str) -> dict:
        """Меняет code на id_token и возвращает проверенные claims."""
        data = await self._request("POST", TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "code_verifier": verifier,
        }, auth=aiohttp.BasicAuth(self.client_id, self.client_secret))
        token = data.get("id_token")
        if not token:
            raise LoginError(f"в ответе /token нет id_token: {data.get('error') or data}")
        try:
            return verify_id_token(token, await self._keys(), self.client_id, nonce)
        except LoginError:
            # Telegram мог сменить ключи — один раз перечитываем JWKS
            return verify_id_token(token, await self._keys(force=True), self.client_id, nonce)

    async def close(self) -> None:
        if self._http:
            await self._http.close()

    async def _keys(self, force: bool = False) -> dict:
        if force or self._jwks is None or time.monotonic() - self._jwks_at > JWKS_TTL:
            self._jwks = await self._request("GET", JWKS_URL)
            self._jwks_at = time.monotonic()
        return self._jwks

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            async with self._http.request(method, url, **kwargs) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            raise LoginError(f"{url}: {e!r}") from e
        if resp.status != 200 or not isinstance(data, dict):
            raise LoginError(f"{url}: HTTP {resp.status} {data}")
        return data
