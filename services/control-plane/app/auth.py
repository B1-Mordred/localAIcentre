from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum


class Role(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    CREATOR = "creator"
    USER = "user"
    SERVICE = "service"


ROLE_SCOPES: dict[Role, set[str]] = {
    Role.ADMIN: {"*"},
    Role.OPERATOR: {
        "admin:read",
        "inference:write",
        "jobs:read",
        "jobs:write",
        "models:read",
        "models:write",
        "runtimes:read",
        "runtimes:write",
        "storage:read",
        "storage:write",
        "workflows:read",
        "workflows:write",
        "benchmarks:read",
        "benchmarks:write",
        "benchmarks:review",
    },
    Role.CREATOR: {
        "jobs:read",
        "jobs:write",
        "models:read",
        "workflows:read",
        "workflows:write",
        "benchmarks:read",
        "benchmarks:review",
    },
    Role.USER: {
        "jobs:read",
        "jobs:write",
        "models:read",
        "workflows:read",
        "inference:write",
    },
    Role.SERVICE: {
        "jobs:read",
        "jobs:write",
        "models:read",
        "modelhub:read",
        "modelhub:sync",
        "workflows:read",
        "inference:write",
    },
}

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 256
PASSWORD_SCRYPT_N = 2**15
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1
PASSWORD_SCRYPT_MAXMEM = 64 * 1024 * 1024


@dataclass(frozen=True)
class AuthContext:
    subject_id: str
    role: Role
    scopes: frozenset[str]
    key_prefix: str | None = None
    session_id: str | None = None
    csrf_token: str | None = None
    default_b1_tools: tuple[str, ...] = ()

    def has_scope(self, required_scope: str) -> bool:
        return "*" in self.scopes or required_scope in self.scopes


def scopes_for_role(role: Role, requested_scopes: list[str] | None = None) -> frozenset[str]:
    allowed = ROLE_SCOPES[role]
    if "*" in allowed:
        return frozenset(requested_scopes or ["*"])
    if requested_scopes is None:
        return frozenset(allowed)
    requested = set(requested_scopes)
    unknown = requested - allowed
    if unknown:
        raise ValueError(f"role {role.value} cannot grant scopes: {', '.join(sorted(unknown))}")
    return frozenset(requested)


def generate_api_key(prefix: str = "b1k") -> tuple[str, str]:
    public = f"{prefix}_{secrets.token_urlsafe(8)}"
    secret = secrets.token_urlsafe(32)
    return public, f"{public}.{secret}"


def hash_api_key(api_key: str, salt: str | None = None) -> tuple[str, str]:
    raw_salt = base64.urlsafe_b64decode(salt.encode("ascii")) if salt else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", api_key.encode("utf-8"), raw_salt, 210_000)
    encoded_salt = base64.urlsafe_b64encode(raw_salt).decode("ascii")
    encoded_digest = base64.urlsafe_b64encode(digest).decode("ascii")
    return encoded_salt, encoded_digest


def verify_api_key(api_key: str, salt: str, expected_hash: str) -> bool:
    _, computed = hash_api_key(api_key, salt)
    return hmac.compare_digest(computed, expected_hash)


def key_prefix_from_token(api_key: str) -> str | None:
    public, sep, _ = api_key.partition(".")
    if not sep or not public.startswith("b1k_"):
        return None
    return public


def password_policy_errors(password: str) -> list[str]:
    errors: list[str] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f"password must be at least {PASSWORD_MIN_LENGTH} characters")
    if len(password) > PASSWORD_MAX_LENGTH:
        errors.append(f"password must be at most {PASSWORD_MAX_LENGTH} characters")
    classes = [
        any(ch.islower() for ch in password),
        any(ch.isupper() for ch in password),
        any(ch.isdigit() for ch in password),
        any(not ch.isalnum() for ch in password),
    ]
    if sum(classes) < 3:
        errors.append("password must include at least three character classes")
    return errors


def hash_password(password: str, salt: str | None = None) -> str:
    raw_salt = base64.urlsafe_b64decode(salt.encode("ascii")) if salt else secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=raw_salt,
        n=PASSWORD_SCRYPT_N,
        r=PASSWORD_SCRYPT_R,
        p=PASSWORD_SCRYPT_P,
        maxmem=PASSWORD_SCRYPT_MAXMEM,
    )
    encoded_salt = base64.urlsafe_b64encode(raw_salt).decode("ascii")
    encoded_digest = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"scrypt${PASSWORD_SCRYPT_N}${PASSWORD_SCRYPT_R}${PASSWORD_SCRYPT_P}${encoded_salt}${encoded_digest}"


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded_hash.split("$", 5)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt.encode("ascii")),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=PASSWORD_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    computed = base64.urlsafe_b64encode(digest).decode("ascii")
    return hmac.compare_digest(computed, expected)


def generate_session_token(prefix: str = "b1s") -> str:
    return f"{prefix}_{secrets.token_urlsafe(48)}"


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(session_token: str) -> str:
    return hashlib.sha256(session_token.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(tz=UTC)
