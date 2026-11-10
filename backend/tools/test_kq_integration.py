"""Booking lookup and Kenya Airways flight data (Priority 4).

The live adapters are exercised against stubbed HTTP responses shaped as in
docs/kq_integration_spec.md; nothing contacts a real service. Runs against a
temporary database.

Run:  python tools/test_kq_integration.py
"""
import json
import sys
import tempfile
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

config.APP_DB_PATH = Path(tempfile.mkdtemp()) / "kq.db"
config.AVIATIONSTACK_API_KEY = ""
config.OPENAI_API_KEY = ""
config.CHAT_MODEL_PROVIDER = "offline"
config.HANDOVER_ENABLED = False
config.KQ_BOOKING_API_URL = ""
config.KQ_FLIGHT_API_URL = ""

from app import agent, aviationstack, booking, database, kq_flights, privacy  # noqa: E402

database.init_db()
_n = [0]


def new_session():
    _n[0] += 1
    return f"cs_kq_{_n[0]}"


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ------------------------------------------------------------------ detection

def test_reference_and_surname_detection():
    cases = {
        "My booking reference is KQDEMO and my surname is Wanjiru": ("KQDEMO", "Wanjiru"),
        "booking ref kqbiz1 under the name Otieno": ("KQBIZ1", "Otieno"),
        "PNR: AB12CD, last name O'Brien": ("AB12CD", "O'Brien"),
    }
    for text, (ref, name) in cases.items():
        assert booking.find_reference(text) == ref, text
        assert booking.find_surname(text) == name, text
    for text in ("Can I change my booking online?", "I need a reference letter for my visa",
                 "What is the booking number format?", "My flight KQ100 is delayed"):
        assert booking.find_reference(text) is None, text


# ------------------------------------------------------------------ lookup

def test_demo_lookup_needs_reference_and_surname():
    s = new_session()
    found = booking.lookup(s, "kqdemo", "wanjiru")
    assert found["available"] and found["source"] == "demo" and not found["is_live"]
    assert found["segments"][0]["flight_number"] == "KQ100"
    wrong = booking.lookup(s, "KQDEMO", "Otieno")
    assert not wrong["available"] and wrong["reason"] == "not_found"
    # Same wording whichever half was wrong, so neither can be confirmed alone.
    assert booking.describe(wrong, "KQDEMO") == booking.describe(
        booking.lookup(s, "ZZ99ZZ", "Wanjiru"), "ZZ99ZZ")


def test_guessing_is_locked_per_session():
    s, other = new_session(), new_session()
    for i in range(config.BOOKING_LOOKUP_MAX_FAILURES):
        assert booking.lookup(s, f"AAAA{i:02d}", "Smith")["reason"] == "not_found"
    locked = booking.lookup(s, "KQDEMO", "Wanjiru")
    assert not locked["available"] and locked["reason"] == "locked", "even a correct guess is refused"
    assert booking.lookup(other, "KQDEMO", "Wanjiru")["available"], "other sessions unaffected"


def test_live_booking_adapter():
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        if json["pnr"] == "AB12CD" and json["surname"] == "Kamau":
            return Resp(200, {"pnr": "AB12CD",
                              "passengers": [{"givenName": "Jane", "surname": "Kamau"}],
                              "segments": [{"flightNumber": "kq412",
                                            "origin": {"iata": "NBO", "city": "Nairobi"},
                                            "destination": {"iata": "MBA", "city": "Mombasa"},
                                            "departureUtc": "2026-12-01T06:00:00Z", "cabin": "Economy",
                                            "fareBrand": "Economy Saver",
                                            "checkedBaggage": {"pieces": 1, "weightKg": 23}}]})
        return Resp(404)

    saved = (config.KQ_BOOKING_API_URL, config.KQ_BOOKING_API_TOKEN, booking.requests.post)
    config.KQ_BOOKING_API_URL, config.KQ_BOOKING_API_TOKEN = "https://kq.example/api", "tok"
    booking.requests.post = post
    try:
        found = booking.lookup(new_session(), "ab12cd", "Kamau")
        assert found["available"] and found["is_live"] and found["source"] == booking.SOURCE_LIVE
        seg = found["segments"][0]
        assert seg["flight_number"] == "KQ412" and seg["checked_baggage"] == {"pieces": 1, "weight_kg": 23}
        assert "Kamau" not in json.dumps(found) and "Jane" not in json.dumps(found), "names are not kept"
        url, body, headers = calls[-1]
        assert url.endswith("/orders/retrieve") and "Kamau" not in url, "surname never in a URL"
        assert headers["Authorization"] == "Bearer tok"
        assert booking.lookup(new_session(), "AB12CD", "Otieno")["reason"] == "not_found"

        def down(*a, **k):
            raise ConnectionError("refused")
        booking.requests.post = down
        s = new_session()
        assert booking.lookup(s, "AB12CD", "Kamau")["reason"] == "unavailable"
        assert booking.failures_in_last_hour(s) == 0, "an outage is not a failed guess"
        assert "Demonstration" not in booking.describe(found, "AB12CD")
    finally:
        config.KQ_BOOKING_API_URL, config.KQ_BOOKING_API_TOKEN, booking.requests.post = saved


# ------------------------------------------------------------------ the trip in answers

def test_trip_fills_in_cabin_route_and_journey_type():
    economy = booking._DEMO[("KQDEMO", "WANJIRU")]
    assert booking.enrich("How many bags can I check?", economy) == \
        "How many bags can I check in Economy from Nairobi to London?"
    assert booking.enrich("When does check-in close?", economy) == \
        "When does check-in close for international flights from Nairobi to London?"
    domestic = [dict(economy[0], destination="Mombasa", destination_code="MBA")]
    assert "for domestic flights" in booking.enrich("When does check-in close?", domestic)
    assert booking.enrich("How many bags can I check in Business?", economy) is None, "cabin given"
    assert booking.enrich("Baggage allowance to Dubai?", economy) is None, "route given"
    assert booking.enrich("How long does a refund take?", economy) is None, "not trip-dependent"
    assert booking.enrich("How many bags can I check?", []) is None


def test_agent_answers_for_the_booked_trip():
    s = new_session()
    asked = agent.run_agent_turn(s, "My booking reference is KQDEMO")
    assert "surname" in asked["answer"] and asked["sources"] == []
    found = agent.run_agent_turn(s, "My booking reference is KQDEMO and my surname is Wanjiru")
    assert "demonstration booking KQDEMO" in found["answer"]
    assert "Wanjiru" not in json.dumps(found["trace"]), "the surname is not copied into the trace"
    bags = agent.run_agent_turn(s, "How many bags can I check?")
    assert bags["answer"].startswith("Your booking on KQ100 (Economy, Economy Classic fare) includes 2")
    assert "two pieces of up to 23kg" in bags["answer"]
    status = agent.run_agent_turn(s, "Is my flight delayed?")
    assert "KQ100" in status["answer"], status["answer"][:200]
    check_in = agent.run_agent_turn(s, "When does check-in close?")
    assert "International check-in counters close one hour before departure" in check_in["answer"]


def test_erasure_includes_booking_data():
    s = new_session()
    agent.run_agent_turn(s, "booking ref KQBIZ1 surname Otieno")
    booking.lookup(s, "WRONG1", "Nobody")
    assert booking.remembered(s) and booking.failures_in_last_hour(s) == 1
    privacy.erase_sessions([s])
    assert booking.remembered(s) == [] and booking.failures_in_last_hour(s) == 0


# ------------------------------------------------------------------ flight data

FEED = {"flightNumber": "KQ100", "status": "DELAYED", "delayMinutes": 40,
        "estimatedDepartureUtc": "2026-11-10T23:20Z",
        "departure": {"iata": "NBO", "name": "Jomo Kenyatta International", "gate": "C3"},
        "arrival": {"iata": "LHR", "name": "London Heathrow"}}


def _with_feed(get, aviationstack_key="", fallback=None):
    saved = (config.KQ_FLIGHT_API_URL, config.AVIATIONSTACK_API_KEY, kq_flights.requests.get,
             aviationstack.get_flight_status)
    config.KQ_FLIGHT_API_URL, config.AVIATIONSTACK_API_KEY = "https://kq.example/flights-api", aviationstack_key
    kq_flights.requests.get = get
    if fallback:
        aviationstack.get_flight_status = fallback
    return saved


def _restore(saved):
    (config.KQ_FLIGHT_API_URL, config.AVIATIONSTACK_API_KEY, kq_flights.requests.get,
     aviationstack.get_flight_status) = saved


def test_kenya_airways_feed_is_used_first():
    saved = _with_feed(lambda url, params=None, headers=None, timeout=None: Resp(200, FEED))
    try:
        r = kq_flights.get_flight_status("kq100")
        assert set(r) == aviationstack.FLIGHT_STATUS_KEYS, "same contract as AviationStack"
        assert r["source"] == "kenya_airways_live" and r["is_live"] and r["available"]
        assert r["gate"] == "C3" and r["delay_minutes"] == 40 and r["status"] == "delayed"
        assert r["route"] == "Jomo Kenyatta International (NBO) -> London Heathrow (LHR)"
        assert "Kenya Airways flight information" in kq_flights.describe(r)
    finally:
        _restore(saved)


def test_not_operating_is_not_looked_up_elsewhere():
    used = []
    saved = _with_feed(lambda *a, **k: Resp(404), aviationstack_key="key",
                       fallback=lambda n: used.append(n) or {})
    try:
        r = kq_flights.get_flight_status("KQ999")
        assert not r["available"] and "no KQ999 operating today" in r["reason"]
        assert used == [], "the airline is the authority on its own schedule"
    finally:
        _restore(saved)


def test_feed_outage_falls_back_then_reports_unavailable():
    def down(*a, **k):
        raise ConnectionError("refused")
    backup = aviationstack._result(aviationstack.SOURCE_LIVE, "KQ100", True, status="scheduled")
    saved = _with_feed(down, aviationstack_key="key", fallback=lambda n: backup)
    try:
        r = kq_flights.get_flight_status("KQ100")
        assert r["source"] == "aviationstack_live" and "AviationStack" in kq_flights.describe(r)
    finally:
        _restore(saved)
    saved = _with_feed(down)
    try:
        r = kq_flights.get_flight_status("KQ100")
        assert not r["available"] and r["source"] == "kenya_airways_live"
        assert "couldn't get the current status" in kq_flights.describe(r)
    finally:
        _restore(saved)


def test_without_the_feed_nothing_changes():
    r = kq_flights.get_flight_status("KQ100")
    assert r["source"] == "demo" and r["available"], "demo flights as before"


def main() -> int:
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception:
            failures += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
