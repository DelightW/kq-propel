"""
Verifies which external integrations are actually live.

Run this after editing .env. It reports, per integration, whether a credential
is present and whether the credential actually works - a key that is present
but rejected is worse than no key, because the client silently falls back to
sandbox data and the system looks healthy while serving fabricated values.

Run:  python tools/check_credentials.py
"""
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

ENV_PATH = BACKEND.parent / ".env"


def _load_env():
    """Minimal .env loader so this works without python-dotenv installed."""
    if not ENV_PATH.exists():
        return False
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())
    return True


def _mask(secret: str) -> str:
    if not secret:
        return "(empty)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-2:]}  ({len(secret)} chars)"


def check_aviationstack() -> None:
    import requests

    key = os.environ.get("AVIATIONSTACK_API_KEY", "").strip()
    print("\nAviationStack")
    print(f"  key            : {_mask(key)}")
    if not key:
        print("  status         : NOT CONFIGURED -> flight data is SANDBOX (fabricated)")
        print("  register at    : https://aviationstack.com/signup/free")
        return

    try:
        resp = requests.get(
            "https://api.aviationstack.com/v1/flights",
            params={"access_key": key, "airline_iata": "KQ", "limit": 1},
            timeout=15,
        )
    except Exception as exc:
        print(f"  status         : NETWORK ERROR -> {exc}")
        print("  effect         : client falls back to SANDBOX data")
        return

    if resp.status_code != 200:
        print(f"  status         : HTTP {resp.status_code} -> {resp.text[:160]}")
        print("  effect         : client falls back to SANDBOX data")
        return

    payload = resp.json()
    if "error" in payload:
        err = payload["error"]
        print(f"  status         : REJECTED -> {err.get('code')}: {err.get('message')}")
        print("  effect         : client falls back to SANDBOX data")
        return

    data = payload.get("data") or []
    print(f"  status         : LIVE - {len(data)} record(s) returned")
    if data:
        f = data[0]
        dep = f.get("departure") or {}
        arr = f.get("arrival") or {}
        print(f"  sample flight  : {(f.get('flight') or {}).get('iata')} "
              f"{dep.get('iata')} -> {arr.get('iata')}  status={f.get('flight_status')}")

        # Confirm the normaliser handles a genuine payload, not just fixtures.
        from app import aviationstack
        norm = aviationstack.normalize_live_flight(
            f, (f.get("flight") or {}).get("iata") or "UNKNOWN")
        missing = set(aviationstack.FLIGHT_STATUS_KEYS) - set(norm)
        print(f"  contract check : {'OK' if not missing else 'MISSING ' + str(missing)}")
        print(f"  normalised     : route={norm['route']!r} gate={norm['gate']!r} "
              f"delay={norm['delay_minutes']}")

    remaining = payload.get("pagination", {})
    if remaining:
        print(f"  pagination     : {remaining}")


def check_simple(label: str, var: str, note: str) -> None:
    value = os.environ.get(var, "").strip()
    print(f"\n{label}")
    print(f"  key            : {_mask(value)}")
    print(f"  status         : {'CONFIGURED' if value else 'NOT CONFIGURED -> ' + note}")


def main() -> int:
    print("=" * 70)
    print("KQ-Propel credential check")
    print("=" * 70)
    found = _load_env()
    print(f".env file        : {ENV_PATH if found else 'NOT FOUND (' + str(ENV_PATH) + ')'}")

    check_aviationstack()
    check_simple("OpenAI", "OPENAI_API_KEY",
                 "hashed embeddings + extractive composer")
    check_simple("Safaricom Daraja", "DARAJA_CONSUMER_KEY",
                 "simulated STK push")
    check_simple("PayPal (card rail)", "PAYPAL_CLIENT_ID",
                 "simulated card checkout link")
    check_simple("MongoDB Atlas", "MONGODB_URI",
                 "local JSON vector store")

    print("\nAnything reported NOT CONFIGURED or REJECTED is serving fabricated")
    print("sandbox values. Do not describe those integrations as live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
