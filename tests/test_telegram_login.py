import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.telegram_login import ISSUER, LoginError, TelegramLogin, new_pkce, phone_from_claims, verify_id_token

CLIENT_ID = "123456"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(key=KEY, kid="k1") -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**jwk, "kid": kid, "alg": "RS256", "use": "sig"}]}


def id_token(key=KEY, kid="k1", **claims) -> str:
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "999", "iat": now, "exp": now + 3600, "nonce": "n1",
               "id": 42, "name": "Иван Иванов", "preferred_username": "ivan",
               "phone_number": "79001234567", "phone_number_verified": True, **claims}
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


def test_valid_token():
    claims = verify_id_token(id_token(), jwks(), CLIENT_ID, "n1")
    assert claims["id"] == 42 and claims["preferred_username"] == "ivan"
    assert verify_id_token(id_token(id="42"), jwks(), CLIENT_ID, "n1")["id"] == 42  # id строкой тоже ок


@pytest.mark.parametrize("token, nonce", [
    (id_token(key=OTHER_KEY), "n1"),                   # подписан не Telegram
    (id_token(kid="k2"), "n1"),                        # неизвестный ключ
    (id_token(aud="777"), "n1"),                       # токен для другого бота
    (id_token(iss="https://evil.example"), "n1"),
    (id_token(exp=int(time.time()) - 3600), "n1"),     # просрочен
    (id_token(), "другой nonce"),
    (id_token(id=None), "n1"),                         # нет Telegram id
    ("мусор", "n1"),
])
def test_invalid_tokens(token, nonce):
    with pytest.raises(LoginError):
        verify_id_token(token, jwks(), CLIENT_ID, nonce)


def test_token_without_nonce_is_accepted():
    now = int(time.time())
    token = jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 60, "id": 5}, KEY,
                       algorithm="RS256", headers={"kid": "k1"})
    assert verify_id_token(token, jwks(), CLIENT_ID, "n1")["id"] == 5


def test_phone_formatting():
    assert phone_from_claims({"phone_number": "79001234567"}) == "(900) 123-45-67"
    assert phone_from_claims({"phone_number": "+7 (900) 123-45-67"}) == "(900) 123-45-67"
    assert phone_from_claims({"phone_number": "89001234567"}) == "(900) 123-45-67"
    assert phone_from_claims({"phone_number": "380501234567"}) is None  # не российский — в форму не подставить
    assert phone_from_claims({"phone_number": "79001234567", "phone_number_verified": False}) is None
    assert phone_from_claims({}) is None


def test_pkce_and_authorize_url():
    verifier, challenge = new_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected and 43 <= len(verifier) <= 128

    url = TelegramLogin(CLIENT_ID, "secret", "https://morelufs.ru/auth/callback").authorize_url("st", challenge, "n")
    parts = urlparse(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert parts.netloc == "oauth.telegram.org" and parts.path == "/auth"
    assert query["client_id"] == CLIENT_ID and query["state"] == "st" and query["code_challenge_method"] == "S256"
    assert set(query["scope"].split()) == {"openid", "profile", "phone", "telegram:bot_access"}
    assert "secret" not in url  # секрет уходит только с сервера на /token
