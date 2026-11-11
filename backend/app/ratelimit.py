"""
Rate limits (Priority 5).

Each limit is a sliding window: at most `limit` events per `window` seconds for
one key (a session, an IP address or a phone number). Counters live in memory,
which suits the single-process deployment: a restart clears them, and no
database write is needed per request (that write would itself be a way to load
the server).

What is limited, and why:

  chat_session    messages per conversation    a person types a few a minute; a
                                               script sends hundreds
  chat_ip         messages per IP address      stops a script that opens a new
                                               session for every message
  session_ip      new sessions per IP          each session is a database row
  mpesa_session   M-Pesa prompts per session   each prompt rings a real phone,
  mpesa_phone     M-Pesa prompts per number    so they must not be usable to
                                               pester someone else's number
  poll_session    status checks per session    the chat polls every few seconds
  receipt_session receipt downloads            each one renders a PDF

The IP limits are generous on purpose: a class or an office behind one
network address shares them.
"""
import math
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

from fastapi import HTTPException, Request, status

from app import config

# bucket -> (limit, window in seconds)
LIMITS: Dict[str, Tuple[int, int]] = {
    "chat_session": (config.RATE_LIMIT_CHAT_PER_MINUTE, 60),
    "chat_ip": (config.RATE_LIMIT_CHAT_PER_MINUTE_PER_IP, 60),
    "session_ip": (config.RATE_LIMIT_SESSIONS_PER_10_MIN_PER_IP, 600),
    "mpesa_session": (config.RATE_LIMIT_MPESA_PROMPTS_PER_10_MIN, 600),
    "mpesa_phone": (config.RATE_LIMIT_MPESA_PROMPTS_PER_10_MIN, 600),
    "poll_session": (120, 60),
    "receipt_session": (20, 60),
}

_events: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
_lock = threading.Lock()


class Limited(Exception):
    def __init__(self, bucket: str, retry_after: int):
        super().__init__(f"{bucket}: retry after {retry_after}s")
        self.bucket, self.retry_after = bucket, retry_after


def hit(bucket: str, key: Optional[str]) -> None:
    """Counts one event, or raises Limited when the window is already full.
    A refused event is not counted, so a client that waits is let back in."""
    if not config.RATE_LIMIT_ENABLED or not key:
        return
    limit, window = LIMITS[bucket]
    now = time.monotonic()
    with _lock:
        q = _events[(bucket, key)]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            raise Limited(bucket, max(1, math.ceil(q[0] + window - now)))
        q.append(now)


def check_http(bucket: str, key: Optional[str]) -> None:
    """hit(), turned into the HTTP answer browsers and clients understand:
    429 with a Retry-After header."""
    try:
        hit(bucket, key)
    except Limited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many requests. Please wait {_wait(exc.retry_after)} and try again.",
            headers={"Retry-After": str(exc.retry_after)},
        )


def _wait(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = math.ceil(seconds / 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def wait_text(exc: Limited) -> str:
    return _wait(exc.retry_after)


def client_ip(request: Request) -> str:
    """The caller's address as the limits see it.

    Behind a proxy every request arrives from the proxy, so the address has to
    come from X-Forwarded-For. Only the entries added by proxies we run behind
    can be trusted - the client can write anything to the left of them - so
    the address used is the one `TRUSTED_PROXY_HOPS` places from the right.
    """
    hops = config.TRUSTED_PROXY_HOPS
    if hops > 0:
        forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return request.client.host if request.client else "unknown"


def reset() -> None:
    """Clears every counter (tests)."""
    with _lock:
        _events.clear()
