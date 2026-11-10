"""
Kenya Airways' own flight-status feed, and the order in which flight sources
are tried.

AviationStack is a third-party aggregator: its gate, status and delay can lag
or disagree with what Kenya Airways shows at the airport. Once the airline
provides its feed (KQ_FLIGHT_API_URL), it is asked first and AviationStack is
the backup. Every answer names the source it came from.

The request and response shapes below are the ones proposed to Kenya Airways
in docs/kq_integration_spec.md. If the real feed differs, only `normalize`
needs to change; the agent relies on the shared contract in aviationstack.py.

  GET {KQ_FLIGHT_API_URL}/flights/{flightNumber}?date=YYYY-MM-DD
  Authorization: Bearer {KQ_FLIGHT_API_TOKEN}

  200 {"flightNumber": "KQ100", "status": "DELAYED", "delayMinutes": 185,
       "scheduledDepartureUtc": "...", "estimatedDepartureUtc": "...",
       "departure": {"iata": "NBO", "name": "Jomo Kenyatta International", "gate": "B14"},
       "arrival": {"iata": "LHR", "name": "London Heathrow"}}
  404 the flight is not operating on that date
"""
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

from app import aviationstack, config

last_error: Optional[str] = None


def _label(end: Dict) -> Optional[str]:
    name, iata = (end.get("name") or "").strip(), (end.get("iata") or "").strip()
    if name and iata:
        return f"{name} ({iata})"
    return name or iata or None


def normalize(body: Dict, flight_number: str) -> Dict:
    """Maps a feed record onto the shared flight-status contract. Missing
    fields stay None rather than becoming a zero delay or an invented gate."""
    dep, arr = body.get("departure") or {}, body.get("arrival") or {}
    origin, destination = _label(dep), _label(arr)
    try:
        delay = int(body["delayMinutes"]) if body.get("delayMinutes") is not None else None
    except (TypeError, ValueError):
        delay = None
    status = body.get("status")
    return aviationstack._result(
        aviationstack.SOURCE_KQ, flight_number, True,
        route=f"{origin} -> {destination}" if origin and destination else None,
        status=status.lower() if isinstance(status, str) and status else None,
        delay_minutes=delay,
        gate=dep.get("gate") or None,
        estimated=body.get("estimatedDepartureUtc") or body.get("scheduledDepartureUtc") or None,
    )


def fetch(flight_number: str) -> Optional[Dict]:
    """The feed's record for today, None when the flight is not operating.
    Raises on any other failure."""
    headers = {"Accept": "application/json"}
    if config.KQ_FLIGHT_API_TOKEN:
        headers["Authorization"] = f"Bearer {config.KQ_FLIGHT_API_TOKEN}"
    resp = requests.get(f"{config.KQ_FLIGHT_API_URL}/flights/{flight_number}",
                        params={"date": datetime.now(timezone.utc).date().isoformat()},
                        headers=headers, timeout=config.KQ_API_TIMEOUT_SECONDS)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return normalize(resp.json(), flight_number)


def get_flight_status(flight_number: str) -> Dict:
    """Kenya Airways' feed first, then AviationStack, then unavailable.

    A flight the airline says is not operating is reported as not found; it is
    not looked up elsewhere, because the airline is the authority on its own
    schedule. Only a failure of the feed itself falls back to AviationStack.
    """
    global last_error
    flight_number = flight_number.upper().strip()
    if not config.KQ_FLIGHT_API_URL:
        return aviationstack.get_flight_status(flight_number)
    try:
        record = fetch(flight_number)
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"
        if config.AVIATIONSTACK_API_KEY:
            return aviationstack.get_flight_status(flight_number)
        return aviationstack._result(aviationstack.SOURCE_KQ, flight_number, False,
                                     reason="Kenya Airways flight information could not be reached.")
    last_error = None
    if record is None:
        return aviationstack._result(aviationstack.SOURCE_KQ, flight_number, False,
                                     reason=f"Kenya Airways has no {flight_number} operating today.")
    return record


describe = aviationstack.describe
