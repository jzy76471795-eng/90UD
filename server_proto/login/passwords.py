from __future__ import annotations

import base64
import hashlib
import hmac
import os


SCHEME = "scrypt-v1"
DEFAULT_N = 1 << 15
DEFAULT_R = 8
DEFAULT_P = 1
DEFAULT_DKLEN = 32
DEFAULT_MAXMEM = 64 * 1024 * 1024


class PasswordFormatError(ValueError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    text = str(value or "").strip()
    if not text:
        raise PasswordFormatError("empty base64 field")
    try:
        return base64.urlsafe_b64decode(text + ("=" * (-len(text) % 4)))
    except Exception as exc:
        raise PasswordFormatError("invalid base64 field") from exc


def validate_password(password: str) -> str:
    value = str(password or "")
    if not 8 <= len(value) <= 128:
        raise ValueError("password length must be 8..128 characters")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("password contains an invalid control character")
    return value


def hash_password(
    password: str,
    *,
    salt: bytes | None = None,
    n: int = DEFAULT_N,
    r: int = DEFAULT_R,
    p: int = DEFAULT_P,
    dklen: int = DEFAULT_DKLEN,
) -> str:
    value = validate_password(password)
    raw_salt = bytes(salt) if salt is not None else os.urandom(16)
    if len(raw_salt) < 16:
        raise ValueError("password salt must contain at least 16 bytes")
    derived = hashlib.scrypt(
        value.encode("utf-8"),
        salt=raw_salt,
        n=int(n),
        r=int(r),
        p=int(p),
        dklen=int(dklen),
        maxmem=DEFAULT_MAXMEM,
    )
    return "$".join(
        (
            SCHEME,
            str(int(n)),
            str(int(r)),
            str(int(p)),
            str(int(dklen)),
            _b64encode(raw_salt),
            _b64encode(derived),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    text = str(encoded or "").strip()
    if not text:
        return False
    parts = text.split("$")
    if len(parts) != 7 or parts[0] != SCHEME:
        return False
    try:
        n, r, p, dklen = (int(value, 10) for value in parts[1:5])
        if n < (1 << 14) or n > (1 << 20) or r < 1 or p < 1:
            raise PasswordFormatError("scrypt parameters outside accepted bounds")
        if dklen < 16 or dklen > 64:
            raise PasswordFormatError("scrypt digest length outside accepted bounds")
        salt = _b64decode(parts[5])
        expected = _b64decode(parts[6])
        if len(salt) < 16 or len(expected) != dklen:
            raise PasswordFormatError("invalid password hash lengths")
        actual = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=dklen,
            maxmem=DEFAULT_MAXMEM,
        )
    except (ValueError, PasswordFormatError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


DUMMY_PASSWORD_HASH = hash_password(
    "usdof-dummy-login-password",
    salt=b"usdof-login-dummy",
)
