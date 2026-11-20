"""Password hashing (Argon2id) and password policy."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import get_settings

_hasher = PasswordHasher()

# A throwaway hash used to equalise timing when the email doesn't exist.
_DUMMY_HASH = _hasher.hash("not-a-real-password-just-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and bool(password_hash)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def password_problems(password: str, email: str = "") -> list[str]:
    s = get_settings()
    problems = []
    if len(password) < s.min_password_length:
        problems.append(f"Use at least {s.min_password_length} characters.")
    if len(password) > 256:
        problems.append("Use at most 256 characters.")
    if email and password.lower() == email.lower():
        problems.append("Don't use your email address as your password.")
    if len(set(password)) < 4:
        problems.append("Use a less repetitive password.")
    return problems
