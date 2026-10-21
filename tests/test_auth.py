from app.auth import verify_init_data
from tests.conftest import TOKEN, make_init_data


def test_valid():
    assert verify_init_data(make_init_data(42), TOKEN)["id"] == 42


def test_wrong_token_or_tampered():
    assert verify_init_data(make_init_data(42, token="1:OTHER"), TOKEN) is None
    assert verify_init_data(make_init_data(42).replace("42", "43"), TOKEN) is None


def test_expired_and_garbage():
    assert verify_init_data(make_init_data(42, age=2 * 86400), TOKEN) is None
    for bad in ("", "hash=1", "a=b&c", "user=x&auth_date=1&hash=0"):
        assert verify_init_data(bad, TOKEN) is None
