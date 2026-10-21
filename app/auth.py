"""Проверка подписи initData Telegram Mini App.

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from app.config import INIT_DATA_TTL


def verify_init_data(init_data: str, bot_token: str, max_age: int = INIT_DATA_TTL) -> dict | None:
    """Возвращает пользователя Telegram из initData или None, если подпись неверна или устарела."""
    try:
        fields = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = fields.pop("hash")
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            return None
        if time.time() - int(fields["auth_date"]) > max_age:
            return None
        user = json.loads(fields["user"])
        return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None
    except (KeyError, ValueError):
        return None
