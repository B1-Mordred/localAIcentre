from __future__ import annotations

import base64
import hashlib
import re
import secrets
from datetime import UTC, datetime
from typing import Any

try:  # pragma: no cover - exercised in the containerized dependency environment
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ModuleNotFoundError:  # pragma: no cover - lets lightweight host tests import the module
    InvalidTag = ValueError  # type: ignore[assignment]
    AESGCM = None  # type: ignore[assignment]


ENVELOPE_SCHEME = "b1-aesgcm-sha256/v1"
SECRET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SECRET_CATEGORIES = {"remote-provider", "model-download", "runtime", "integration", "other"}
MASTER_KEY_MIN_BYTES = 32
SECRET_VALUE_MAX_BYTES = 64 * 1024


class SecretStoreError(ValueError):
    pass


def validate_secret_name(name: str) -> str:
    normalized = name.strip()
    if not SECRET_NAME_PATTERN.fullmatch(normalized):
        raise SecretStoreError("secret name must start with an alphanumeric character and contain only letters, numbers, '.', '_', ':', or '-'")
    return normalized


def validate_secret_category(category: str) -> str:
    if category not in SECRET_CATEGORIES:
        raise SecretStoreError(f"unsupported secret category: {category}")
    return category


def _require_crypto() -> None:
    if AESGCM is None:
        raise SecretStoreError("cryptography is not installed")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise SecretStoreError("encrypted secret envelope contains invalid base64") from exc


def _master_key_material(master_key: str) -> bytes:
    raw = master_key.strip().encode("utf-8")
    if len(raw) < MASTER_KEY_MIN_BYTES:
        raise SecretStoreError(f"master encryption key must be at least {MASTER_KEY_MIN_BYTES} bytes")
    return hashlib.sha256(raw).digest()


def master_key_fingerprint(master_key: str) -> str:
    raw = master_key.strip().encode("utf-8")
    if len(raw) < MASTER_KEY_MIN_BYTES:
        raise SecretStoreError(f"master encryption key must be at least {MASTER_KEY_MIN_BYTES} bytes")
    return hashlib.sha256(raw).hexdigest()[:16]


def _aad(name: str) -> bytes:
    return f"{ENVELOPE_SCHEME}:{validate_secret_name(name)}".encode("utf-8")


def encrypt_value(master_key: str, name: str, plaintext: str, *, created_at: datetime | None = None) -> dict[str, Any]:
    _require_crypto()
    normalized_name = validate_secret_name(name)
    raw_plaintext = plaintext.encode("utf-8")
    if not raw_plaintext:
        raise SecretStoreError("secret value cannot be empty")
    if len(raw_plaintext) > SECRET_VALUE_MAX_BYTES:
        raise SecretStoreError(f"secret value exceeds {SECRET_VALUE_MAX_BYTES} bytes")
    key = _master_key_material(master_key)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, raw_plaintext, _aad(normalized_name))
    created = created_at or datetime.now(tz=UTC)
    return {
        "scheme": ENVELOPE_SCHEME,
        "key_id": master_key_fingerprint(master_key),
        "nonce": _b64encode(nonce),
        "ciphertext": _b64encode(ciphertext),
        "created_at": created.isoformat(),
    }


def decrypt_value(master_key: str, name: str, envelope: dict[str, Any]) -> str:
    _require_crypto()
    normalized_name = validate_secret_name(name)
    if envelope.get("scheme") != ENVELOPE_SCHEME:
        raise SecretStoreError("unsupported encrypted secret envelope scheme")
    key_id = envelope.get("key_id")
    expected_key_id = master_key_fingerprint(master_key)
    if key_id != expected_key_id:
        raise SecretStoreError("encrypted secret was created with a different master key")
    nonce = _b64decode(str(envelope.get("nonce", "")))
    ciphertext = _b64decode(str(envelope.get("ciphertext", "")))
    try:
        plaintext = AESGCM(_master_key_material(master_key)).decrypt(nonce, ciphertext, _aad(normalized_name))
    except (InvalidTag, ValueError) as exc:
        raise SecretStoreError("encrypted secret could not be decrypted") from exc
    return plaintext.decode("utf-8")


def public_secret_record(row: dict[str, Any]) -> dict[str, Any]:
    envelope = row.get("secret_envelope") or {}
    return {
        "name": row.get("name"),
        "display_name": row.get("display_name"),
        "category": row.get("category"),
        "description": row.get("description") or "",
        "scheme": envelope.get("scheme"),
        "key_id": envelope.get("key_id"),
        "encrypted": bool(envelope.get("ciphertext")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "deleted_at": row.get("deleted_at"),
    }
