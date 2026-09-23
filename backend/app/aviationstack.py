"""
AviationStack flight telemetry client.

Uses the live AviationStack API when AVIATIONSTACK_API_KEY is configured;
otherwise returns deterministic sandbox flight data so the agent's tool-use
loop can be fully exercised offline.

Both paths return the identical key set (see FLIGHT_STATUS_KEYS). The live
branch previously returned AviationStack's raw `departure`/`arrival` objects
while the agent reads `route`, `gate` and `delay_minutes`, so configuring a
real API key would have *degraded* answers to "route n/a, gate n/a, delay 0
minutes". The live payload is now normalised into the same contract, and
tools/test_flight_contract.py asserts the two paths cannot drift apart.
"""
import random
from datetime import datetime, timedelta
from typing import Dict, Optional

import requests

from app import config

# The contract both the live and sandbox paths must satisfy, and which
# agent.py relies on when composing a flight-status answer.
FLIGHT_STATUS_KEYS = frozenset({
    "source", "flight_number", "route", "status", "delay_minutes", "gate",
    "estimated_departure_utc",
})

_SANDBOX_FLIGHTS = {
    "KQ100": {"route": "Nairobi (NBO) -> London (LHR)", "status": "delayed", "delay_minutes": 185, "gate": "B14"},
    "KQ310": {"route": "Nairobi (NBO) -> Mombasa (MBA)", "status": "scheduled", "delay_minutes": 0, "gate": "A3"},
    "KQ203": {"route": "Nairobi (NBO) -> Amsterdam (AMS)", "status": "boarding", "delay_minutes": 15, "gate": "C7"},
}


def _endpoint_label(endpoint: Dict) -> str:
    """'Nairobi (NBO)' from an AviationStack departure/arrival object."""
    airport = (endpoint.get("airport") or "").strip()
    iata = (endpoint.get("iata") or "").strip()
    if airport and iata:
        return f"{airport} ({iata})"
    return airport or iata or "Unknown"


def normalize_live_flight(flight: Dict, flight_number: str) -> Dict:
    """Maps an AviationStack record onto the shared flight-status contract."""
    departure = flight.get("departure") or {}
    arrival = flight.get("arrival") or {}

    delay = departure.get("delay")
    try:
        delay_minutes = int(delay) if delay is not None else 0
    except (TypeError, ValueError):
        delay_minutes = 0

    estimated = (departure.get("estimated") or departure.get("scheduled") or "")

    return {
        "source": "aviationstack_live",
        "flight_number": flight_number,
        "route": f"{_endpoint_label(departure)} -> {_endpoint_label(arrival)}",
        "status": flight.get("flight_status") or "unknown",
        "delay_minutes": delay_minutes,
        "gate": (departure.get("gate") or "TBD"),
        "estimated_departure_utc": estimated,
    }


def _sandbox_flight(flight_number: str, source: str = "sandbox") -> Dict:
    sandbox = _SANDBOX_FLIGHTS.get(flight_number)
    if not sandbox:
        sandbox = {
            "route": "Unknown route",
            "status": random.choice(["scheduled", "delayed", "boarding"]),
            "delay_minutes": random.choice([0, 20, 90, 200]),
            "gate": "TBD",
        }
    eta = datetime.utcnow() + timedelta(minutes=sandbox["delay_minutes"])
    return {
        "source": source,
        "flight_number": flight_number,
        "route": sandbox["route"],
        "status": sandbox["status"],
        "delay_minutes": sandbox["delay_minutes"],
        "gate": sandbox["gate"],
        "estimated_departure_utc": eta.isoformat(timespec="minutes"),
    }


def _fetch_live(flight_number: str) -> Optional[Dict]:
    resp = requests.get(
        "https://api.aviationstack.com/v1/flights",
        params={"access_key": config.AVIATIONSTACK_API_KEY, "flight_iata": flight_number},
        timeout=8,
    )
    resp.raise_for_status()
    data = resp.json().get("data") or []
    if not data:
        return None
    return normalize_live_flight(data[0], flight_number)


last_live_error: Optional[str] = None


def get_flight_status(flight_number: str) -> Dict:
    """Returns live telemetry when a key is configured, sandbox data otherwise.

    A configured key that then fails must not be indistinguishable from having
    no key at all: swallowing the error silently is what let an inert .env
    loader masquerade as a working integration. The fallback therefore reports
    `source="sandbox_after_live_error"` and the reason is retained in
    `last_live_error`, while the key set stays inside FLIGHT_STATUS_KEYS.
    """
    global last_live_error
    flight_number = flight_number.upper().strip()
    if config.AVIATIONSTACK_API_KEY:
        try:
            live = _fetch_live(flight_number)
            last_live_error = None
            if live:
                return live
            last_live_error = f"no record returned for {flight_number}"
        except Exception as exc:
            last_live_error = f"{type(exc).__name__}: {exc}"
        return _sandbox_flight(flight_number, source="sandbox_after_live_error")

    return _sandbox_flight(flight_number)
