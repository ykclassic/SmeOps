import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


def hash_whatsapp_contact(phone: str) -> str:
    key = get_settings().app_secret_key.get_secret_value().encode()
    return hmac.new(key, phone.encode(), hashlib.sha256).hexdigest()


def _fernet() -> Fernet:
    key = get_settings().whatsapp_token_encryption_key
    if key is None:
        raise RuntimeError("WhatsApp token encryption is not configured")
    try:
        return Fernet(key.get_secret_value().encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError("WhatsApp token encryption key is invalid") from exc


def encrypt_whatsapp_secret(value: str) -> str:
    if not value:
        raise ValueError("Cannot encrypt an empty WhatsApp secret")
    return _fernet().encrypt(value.encode()).decode()


def decrypt_whatsapp_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("WhatsApp secret could not be decrypted") from exc
