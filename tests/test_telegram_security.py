from app.core.telegram import (
    hash_telegram_webhook_secret,
    verify_telegram_webhook_secret,
)


def test_webhook_secret_hash_is_sha256_and_verifies():
    secret = "a-long-random-webhook-secret"
    digest = hash_telegram_webhook_secret(secret)

    assert len(digest) == 64
    assert verify_telegram_webhook_secret(secret, digest)
    assert not verify_telegram_webhook_secret("wrong-secret", digest)
    assert not verify_telegram_webhook_secret("", digest)


def test_webhook_secret_hash_rejects_empty_or_oversized_values():
    import pytest

    with pytest.raises(ValueError):
        hash_telegram_webhook_secret("")
    with pytest.raises(ValueError):
        hash_telegram_webhook_secret("x" * 257)
