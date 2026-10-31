from uuid import uuid4

import jwt
import pytest

from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hash_is_not_plaintext_and_verifies() -> None:
    password = "Correct-Horse-Battery-42"
    password_hash = hash_password(password)

    assert password_hash != password
    assert verify_password(password, password_hash)
    assert not verify_password("incorrect-password", password_hash)


def test_access_token_round_trip() -> None:
    user_id = uuid4()
    tenant_id = uuid4()

    token = create_access_token(user_id=user_id, tenant_id=tenant_id)

    assert decode_access_token(token) == (user_id, tenant_id)


def test_access_token_rejects_tampering() -> None:
    token = create_access_token(user_id=uuid4(), tenant_id=uuid4())
    tampered = f"{token[:-1]}{'a' if token[-1] != 'a' else 'b'}"

    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(tampered)
