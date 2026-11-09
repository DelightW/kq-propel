"""
AviationStack flight telemetry client.

Two modes, chosen by whether AVIATIONSTACK_API_KEY is configured:

  * Live mode. Results come only from AviationStack. If the request fails or
    the flight is not found, the result is marked unavailable. It never falls
    back to demonstration data: a provider outage or a mistyped flight number
    must not become plausible but false operational advice.

  * Demo mode (no key). Three fixed demonstration flights (KQ100, KQ310,
    KQ203) return labelled demonstration data so the agent's tool loop can be
    shown offline. Any other flight is reported as not found. Earlier versions
    invented a random status and delay for unknown flights.

Every result satisfies the same contract (FLIGHT_STATUS_KEYS), states where it
came from (`source`, `is_live`), whether it holds real data (`available`) and
when it was retrieved. Values the provider did not supply stay None; an unknown
delay is never reported as zero minutes. tools/test_flight_contract.py holds
these rules in place.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import requests

from app import config

# The contract every result satisfies, and which agent.py relies on when
# composing a flight-status answer.
FLIGHT_STATUS_KEYS = frozenset({
    "source", "is_live", "available", "flight_number", "route", "status",
    "delay_minutes", "gate", "estimated_departure_utc", "retrieved_at_utc", "reason",
})

SOURCE_LIVE = "aviationstack_live"
SOURCE_DEMO = "demo"

_DEMO_FLIGHTS = {
    "KQ100": {"route": "Nairobi (NBO) -> London (LHR)", "status": "delayed", "delay_minutes": 185, "gate": "B14"},
    "KQ310": {"route": "Nairobi (NBO) -> Mombasa (MBA)", "status": "scheduled", "delay_minutes": 0, "gate": "A3"},
    "KQ203": {"route": "Nairobi (NBO) -> Amsterdam (AMS)", "status": "boarding", "delay_minutes": 15, "gate": "C7"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%MZ")


def _endpoint_label(endpoint: Dict) -> Optional[str]:
    """'Jomo Kenyatta International (NBO)' from an AviationStack departure or
    arrival object, or None when neither name nor code was supplied."""
    airport = (endpoint.get("airport") or "").strip()
    iata = (endpoint.get("iata") or "").strip()
    if airport and iata:
        return f"{airport} ({iata})"
    return airport or iata or None


def _result(source: str, flight_number: str, available: bool, *, route=None, status=None,
            delay_minutes=None, gate=None, estimated=None, reason=None) -> Dict:
    return {
        "source": source,
        "is_live": source == SOURCE_LIVE,
        "available": available,
        "flight_number": flight_number,
        "route": route,
        "status": status,
        "delay_minutes": delay_minutes,
        "gate": gate,
        "estimated_departure_utc": estimated,
        "retrieved_at_utc": _stamp(_now()),
        "reason": reason,
    }


def normalize_live_flight(flight: Dict, flight_number: str) -> Dict:
    """Maps an AviationStack record onto the shared flight-status contract.

    Fields the provider leaves empty stay None, so the agent says nothing about
    them rather than reporting a zero-minute delay or an invented gate.
    """
    departure = flight.get("departure") or {}
    arrival = flight.get("arrival") or {}

    delay = departure.get("delay")
    try:
        delay_minutes = int(delay) if delay is not None else None
    except (TypeError, ValueError):
        delay_minutes = None

    origin, destination = _endpoint_label(departure), _endpoint_label(arrival)
    route = f"{origin} -> {destination}" if origin and destination else None

    return _result(
        SOURCE_LIVE, flight_number, True,
        route=route,
        status=flight.get("flight_status") or None,
        delay_minutes=delay_minutes,
        gate=departure.get("gate") or None,
        estimated=departure.get("estimated") or departure.get("scheduled") or None,
    )


def _demo_flight(flight_number: str) -> Dict:
    demo = _DEMO_FLIGHTS.get(flight_number)
    if not demo:
        return _result(SOURCE_DEMO, flight_number, False,
                       reason=f"{flight_number} is not one of the demonstration flights "
                              f"(KQ100, KQ310, KQ203), and live flight data is not configured.")
    eta = _now() + timedelta(minutes=demo["delay_minutes"])
    return _result(SOURCE_DEMO, flight_number, True, route=demo["route"], status=demo["status"],
                   delay_minutes=demo["delay_minutes"], gate=demo["gate"], estimated=_stamp(eta))


def _fetch_live(flight_number: str) -> Optional[Dict]:
    resp = requests.get(
        "https://api.aviationstack.com/v1/flights",
        params={"access_key": config.AVIATIONSTACK_API_KEY, "flight_iata": flight_number},
        timeout=8,
    )
    resp.raise_for_status()
    body = resp.json()
    # AviationStack reports some failures (bad key, exhausted quota) inside a
    # 200 response rather than through the status code.
    if body.get("error"):
        raise RuntimeError(body["error"].get("info") or body["error"].get("code") or "provider error")
    data = body.get("data") or []
    if not data:
        return None
    return normalize_live_flight(data[0], flight_number)


last_live_error: Optional[str] = None


def get_flight_status(flight_number: str) -> Dict:
    """Live data when a key is configured, demonstration data otherwise.

    In live mode a failure is reported as unavailable, with the reason kept in
    `last_live_error` for the operator. It is never replaced by demonstration
    data.
    """
    global last_live_error
    flight_number = flight_number.upper().strip()
    if not config.AVIATIONSTACK_API_KEY:
        return _demo_flight(flight_number)

    try:
        live = _fetch_live(flight_number)
    except Exception as exc:
        last_live_error = f"{type(exc).__name__}: {exc}"
        return _result(SOURCE_LIVE, flight_number, False,
                       reason="The live flight data service could not be reached.")
    if live is None:
        last_live_error = f"no record returned for {flight_number}"
        return _result(SOURCE_LIVE, flight_number, False,
                       reason=f"No current flight was found for {flight_number}.")
    last_live_error = None
    return live


def describe(status: Dict) -> str:
    """The passenger-facing sentence for a flight-status result.

    Wording follows the source: live data names its provider and retrieval
    time, demonstration data says it is not live, and an unavailable result
    says so and points to the official source instead of guessing.
    """
    number = status.get("flight_number") or "that flight"
    if not status.get("available"):
        reason = status.get("reason") or "Flight status is not available right now."
        return (f"I couldn't get the current status of {number}. {reason} Please check the "
                f"Kenya Airways flight status page or the airport departure screens.")

    details = []
    if status.get("route"):
        details.append(f"route {status['route']}")
    if status.get("status"):
        details.append(f"status: {status['status']}")
    if status.get("gate"):
        details.append(f"gate {status['gate']}")
    delay = status.get("delay_minutes")
    if delay is not None:
        details.append("no delay reported" if delay == 0 else f"delayed by {delay} minutes")
    if status.get("estimated_departure_utc"):
        details.append(f"estimated departure {status['estimated_departure_utc']}")
    body = "; ".join(details) if details else "no details were provided"

    if status.get("is_live"):
        return (f"Live status for {number} (AviationStack, retrieved "
                f"{status.get('retrieved_at_utc')}): {body}.")
    return (f"Demonstration data for {number} - this is not live flight information: "
            f"{body}.")
