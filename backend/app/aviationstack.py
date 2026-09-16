"""
AviationStack flight telemetry client.

Uses the live AviationStack API when AVIATIONSTACK_API_KEY is configured;
otherwise returns deterministic sandbox flight data so the agent's tool-use
loop can be fully exercised offline.
"""
import random
from datetime import datetime, timedelta
from typing import Dict

import requests

from app import config

_SANDBOX_FLIGHTS = {
    "KQ100": {"route": "Nairobi (NBO) -> London (LHR)", "status": "delayed", "delay_minutes": 185, "gate": "B14"},
    "KQ310": {"route": "Nairobi (NBO) -> Mombasa (MBA)", "status": "scheduled", "delay_minutes": 0, "gate": "A3"},
    "KQ203": {"route": "Nairobi (NBO) -> Amsterdam (AMS)", "status": "boarding", "delay_minutes": 15, "gate": "C7"},
}


def get_flight_status(flight_number: str) -> Dict:
    flight_number = flight_number.upper().strip()
    if config.AVIATIONSTACK_API_KEY:
        try:
            resp = requests.get(
                "https://api.aviationstack.com/v1/flights",
                params={"access_key": config.AVIATIONSTACK_API_KEY, "flight_iata": flight_number},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if data:
                flight = data[0]
                return {
                    "source": "aviationstack_live",
                    "flight_number": flight_number,
                    "status": flight.get("flight_status"),
                    "departure": flight.get("departure", {}),
                    "arrival": flight.get("arrival", {}),
                }
        except Exception:
            pass  # fall through to sandbox data

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
        "source": "sandbox",
        "flight_number": flight_number,
        "route": sandbox["route"],
        "status": sandbox["status"],
        "delay_minutes": sandbox["delay_minutes"],
        "gate": sandbox["gate"],
        "estimated_departure_utc": eta.isoformat(timespec="minutes"),
    }
