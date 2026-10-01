"""
Staff authentication and passenger session ownership.

Two separate problems are solved here, both instances of the same defect: the
application previously trusted identity claims it had never issued.

1. Every /api/admin route was anonymous. Anyone who could reach the API could
   read the transaction ledger - including passenger phone numbers - and
   trigger the model comparison. Hiding the link in the UI protected nothing.

2. The chat client invented its own session_id and the server accepted it.
   Conversation history *and pending payment state* are keyed on that value,
   so guessing or reusing another passenger's identifier handed over their
   payment context.

Design notes
------------
Passwords are hashed with scrypt from the standard library. No plaintext
password is stored anywhere, and no new dependency is introduced - bcrypt and
argon2 both need wheels that are unreliable on this Python version.

Session tokens are opaque random values, and only their SHA-256 digest is
persisted. A database leak therefore does not yield usable tokens. They are
server-issued and revocable, which a stateless signed token would not be.
"""
import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Dict, Optional, Tuple

from fastapi import Cookie, Depends, HTTPException, Request, status

from app import config, database

# scrypt cost parameters. n=2**14 keeps a single verification near 50-100ms,
# which is slow enough to make offline guessing expensive and fast enough that
# a login does not feel sluggish.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

ADMIN_COOKIE = "kq_staff"
CHAT_COOKIE = "kq_session"


# --------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------

def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """Returns a self-describing scrypt hash: scrypt$n$r$p$salt$key."""
    if salt is None:
        salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                         r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(key).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verification against a stored scrypt hash."""
    try:
        scheme, n, r, p, salt_b64, key_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n),
                                r=int(r), p=int(p), dklen=len(expected))
    except Exception:
        return False
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------------
# Credential resolution
# --------------------------------------------------------------------------

class _Credential:
    """The configured staff credential and how it was obtained.

    `ephemeral` means no credential was configured and one was generated for
    this process only. That is deliberately noisy rather than convenient: a
    hard-coded default password in source would be a worse vulnerability than
    the one this module exists to fix.
    """

    def __init__(self):
        self.username = config.ADMIN_USERNAME
        self.ephemeral = False
        self.generated_password: Optional[str] = None
        self.source = "none"

        if config.ADMIN_PASSWORD_HASH:
            self.password_hash = config.ADMIN_PASSWORD_HASH
            self.source = "ADMIN_PASSWORD_HASH"
        elif config.ADMIN_PASSWORD:
            self.password_hash = hash_password(config.ADMIN_PASSWORD)
            self.source = "ADMIN_PASSWORD"
        else:
            self.generated_password = secrets.token_urlsafe(12)
            self.password_hash = hash_password(self.generated_password)
            self.ephemeral = True
            self.source = "generated"

    def describe(self) -> Dict[str, object]:
        return {"username": self.username, "source": self.source,
                "ephemeral": self.ephemeral}


_credential: Optional[_Credential] = None


def credential() -> _Credential:
    global _credential
    if _credential is None:
        _credential = _Credential()
    return _credential


def startup_banner() -> str:
    """Printed once at startup so an ephemeral password is usable."""
    cred = credential()
    if not cred.ephemeral:
        return (f"[auth] staff login enabled for '{cred.username}' "
                f"(credential from {cred.source})")
    return (
        "\n" + "=" * 68 +
        "\n[auth] No ADMIN_PASSWORD_HASH or ADMIN_PASSWORD was configured."
        "\n       A one-time password has been generated for THIS RUN ONLY."
        "\n"
        f"\n         username: {cred.username}"
        f"\n         password: {cred.generated_password}"
        "\n"
        "\n       It changes on every restart. To set a permanent one:"
        "\n         python -m app.auth <your-password>"
        "\n       and put the printed line in your .env file."
        "\n" + "=" * 68
    )


# --------------------------------------------------------------------------
# Login throttling
# --------------------------------------------------------------------------
# Held in memory: a single-process prototype does not need shared state, and a
# database write per failed guess would itself be a denial-of-service lever.
_failures: Dict[str, Tuple[int, float]] = {}


def _throttle_key(username: str, ip: str) -> str:
    return f"{username.lower()}|{ip}"


def lockout_remaining(username: str, ip: str) -> int:
    count, first_at = _failures.get(_throttle_key(username, ip), (0, 0.0))
    if count < config.ADMIN_MAX_LOGIN_ATTEMPTS:
        return 0
    elapsed = time.time() - first_at
    remaining = config.ADMIN_LOCKOUT_SECONDS - elapsed
    if remaining <= 0:
        _failures.pop(_throttle_key(username, ip), None)
        return 0
    return int(remaining) + 1


def record_failure(username: str, ip: str) -> None:
    key = _throttle_key(username, ip)
    count, first_at = _failures.get(key, (0, time.time()))
    if time.time() - first_at > config.ADMIN_LOCKOUT_SECONDS:
        count, first_at = 0, time.time()
    _failures[key] = (count + 1, first_at)


def clear_failures(username: str, ip: str) -> None:
    _failures.pop(_throttle_key(username, ip), None)


# --------------------------------------------------------------------------
# Session tokens
# --------------------------------------------------------------------------

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_staff_session(username: str, ip: str, user_agent: str) -> str:
    token = secrets.token_urlsafe(32)
    database.create_admin_session(
        token_hash=_digest(token), username=username,
        ttl_minutes=config.ADMIN_SESSION_TTL_MINUTES,
        ip=ip, user_agent=user_agent[:200],
    )
    return token


def revoke_staff_session(token: str) -> None:
    database.delete_admin_session(_digest(token))


def resolve_staff_session(token: Optional[str]) -> Optional[Dict]:
    if not token:
        return None
    return database.get_admin_session(_digest(token))


def client_ip(request: Request) -> str:
    return (request.client.host if request.client else "unknown")


# --------------------------------------------------------------------------
# FastAPI dependencies
# --------------------------------------------------------------------------

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Staff authentication required.",
)


def require_staff(kq_staff: Optional[str] = Cookie(default=None)) -> Dict:
    """Guards every administrative route.

    Returns the session record so handlers can attribute actions to a user.
    """
    session = resolve_staff_session(kq_staff)
    if not session:
        raise _UNAUTHENTICATED
    database.touch_admin_session(_digest(kq_staff))
    return session


def optional_staff(kq_staff: Optional[str] = Cookie(default=None)) -> Optional[Dict]:
    return resolve_staff_session(kq_staff)


# --------------------------------------------------------------------------
# Passenger chat sessions
# --------------------------------------------------------------------------

def issue_chat_session(ip: str) -> str:
    """Server-issued, unguessable passenger session identifier.

    The client no longer chooses this value, so one passenger cannot address
    another's conversation or pending payment by supplying their identifier.
    """
    session_id = "cs_" + secrets.token_urlsafe(24)
    database.create_chat_session(session_id, ip)
    return session_id


def chat_session_is_valid(session_id: Optional[str]) -> bool:
    if not session_id:
        return False
    return database.get_chat_session(session_id) is not None


def cookie_kwargs(max_age: Optional[int] = None) -> Dict[str, object]:
    """Shared hardening flags for both cookies.

    httponly blocks script access, so a cross-site scripting bug cannot steal
    the session. samesite='lax' stops another site driving authenticated
    requests. secure is configurable because it would break plain-HTTP local
    demonstration, but must be enabled behind TLS.
    """
    kwargs: Dict[str, object] = {
        "httponly": True,
        "samesite": "lax",
        "secure": config.SECURE_COOKIES,
        "path": "/",
    }
    if max_age is not None:
        kwargs["max_age"] = max_age
    return kwargs


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m app.auth <password>")
        raise SystemExit(1)
    print("\nAdd this line to your .env file:\n")
    print("ADMIN_PASSWORD_HASH=" + hash_password(sys.argv[1]))
    print()
