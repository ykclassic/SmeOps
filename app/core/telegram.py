import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


def hash_telegram_webhook_secret(secret: str) -> str:
    if not secret or len(secret) > 256:
        raise ValueError("Telegram webhook secret must contain 1 to 256 characters")
    return hashlib.sha256(secret.encode()).hexdigest()


def verify_telegram_webhook_secret(secret: str, expected_hash: str) -> bool:
    if not secret or len(secret) > 256 or len(expected_hash) != 64:
        return False
    return hmac.compare_digest(hash_telegram_webhook_secret(secret), expected_hash)


def _fernet() -> Fernet:
    key = get_settings().whatsapp_token_encryption_key
    if key is None:
        raise RuntimeError("Channel token encryption is not configured")
    try:
        return Fernet(key.get_secret_value().encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Channel token encryption key is invalid") from exc


def encrypt_telegram_secret(value: str) -> str:
    if not value:
        raise ValueError("Cannot encrypt an empty Telegram secret")
    return _fernet().encrypt(value.encode()).decode()


def decrypt_telegram_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("Telegram secret could not be decrypted") from exc
