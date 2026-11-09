"""
Contract tests for the flight telemetry client.

The live and sandbox paths must expose an identical key set. They diverged
once - the live branch returned AviationStack's raw departure/arrival objects
while the agent reads route/gate/delay_minutes - which meant adding a real API
key would have silently degraded every flight answer to "n/a". These tests make
that class of drift impossible to reintroduce unnoticed.

Run:  python tools/test_flight_contract.py
      pytest tools/test_flight_contract.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import aviationstack, config  # noqa: E402

# Tests never contact AviationStack. Demo-mode tests run with no key; the
# live-mode tests install a key together with a fake HTTP response.
config.AVIATIONSTACK_API_KEY = ""

# A realistic AviationStack /v1/flights record.
LIVE_RECORD = {
    "flight_date": "2026-09-23",
    "flight_status": "active",
    "departure": {"airport": "Jomo Kenyatta International", "iata": "NBO",
                  "gate": "B14", "delay": 185,
                  "scheduled": "2026-09-23T08:20:00+00:00",
                  "estimated": "2026-09-23T11:25:00+00:00"},
    "arrival": {"airport": "Heathrow", "iata": "LHR", "gate": "7", "delay": None},
    "flight": {"iata": "KQ100"},
}


def test_sandbox_matches_declared_contract():
    result = aviationstack.get_flight_status("KQ100")
    assert set(result) == set(aviationstack.FLIGHT_STATUS_KEYS)


def test_live_matches_declared_contract():
    result = aviationstack.normalize_live_flight(LIVE_RECORD, "KQ100")
    assert set(result) == set(aviationstack.FLIGHT_STATUS_KEYS)


def test_live_and_sandbox_key_sets_are_identical():
    live = aviationstack.normalize_live_flight(LIVE_RECORD, "KQ100")
    sandbox = aviationstack.get_flight_status("KQ100")
    assert set(live) == set(sandbox)


def test_live_fields_the_agent_reads_are_populated():
    live = aviationstack.normalize_live_flight(LIVE_RECORD, "KQ100")
    assert live["route"] == "Jomo Kenyatta International (NBO) -> Heathrow (LHR)"
    assert live["gate"] == "B14"
    assert live["delay_minutes"] == 185
    assert live["status"] == "active"
    assert live["source"] == "aviationstack_live"


def test_live_tolerates_missing_and_null_fields():
    sparse = {"flight_status": None, "departure": {}, "arrival": {}}
    live = aviationstack.normalize_live_flight(sparse, "KQ999")
    assert set(live) == set(aviationstack.FLIGHT_STATUS_KEYS)
    # Unknown values stay unknown. A null delay used to become 0, which told
    # the passenger their flight was on time when the provider had said nothing.
    assert live["delay_minutes"] is None
    assert live["gate"] is None
    assert live["status"] is None
    assert live["route"] is None


def test_live_falls_back_to_scheduled_when_no_estimate():
    record = dict(LIVE_RECORD)
    record["departure"] = {"iata": "NBO", "scheduled": "2026-09-23T08:20:00+00:00"}
    live = aviationstack.normalize_live_flight(record, "KQ100")
    assert live["estimated_departure_utc"] == "2026-09-23T08:20:00+00:00"


def test_unknown_flight_still_satisfies_contract():
    result = aviationstack.get_flight_status("ZZ999")
    assert set(result) == set(aviationstack.FLIGHT_STATUS_KEYS)


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


def _with_live_key(fake_get):
    """Runs the live path against a fake HTTP response, restoring state after."""
    from app import config
    saved_key, saved_get = config.AVIATIONSTACK_API_KEY, aviationstack.requests.get
    config.AVIATIONSTACK_API_KEY = "test-key"
    aviationstack.requests.get = fake_get
    try:
        return aviationstack.get_flight_status("KQ100")
    finally:
        config.AVIATIONSTACK_API_KEY, aviationstack.requests.get = saved_key, saved_get


def test_live_outage_is_unavailable_never_demo_data():
    def boom(*a, **k):
        raise ConnectionError("network down")
    result = _with_live_key(boom)
    assert result["available"] is False
    assert result["source"] == aviationstack.SOURCE_LIVE
    assert result["status"] is None and result["delay_minutes"] is None
    assert "B14" not in aviationstack.describe(result)   # demo gate must not leak


def test_live_unknown_flight_is_unavailable():
    result = _with_live_key(lambda *a, **k: _FakeResponse({"data": []}))
    assert result["available"] is False
    assert "No current flight was found" in result["reason"]


def test_provider_error_inside_200_is_unavailable():
    body = {"error": {"code": "usage_limit_reached", "info": "quota exhausted"}}
    result = _with_live_key(lambda *a, **k: _FakeResponse(body))
    assert result["available"] is False
    assert "quota" in (aviationstack.last_live_error or "")


def test_live_success_is_labelled_live_with_time():
    result = _with_live_key(lambda *a, **k: _FakeResponse({"data": [LIVE_RECORD]}))
    text = aviationstack.describe(result)
    assert result["is_live"] and result["available"]
    assert text.startswith("Live status for KQ100 (AviationStack, retrieved")
    assert "delayed by 185 minutes" in text and "gate B14" in text


def test_demo_data_is_never_called_live():
    text = aviationstack.describe(aviationstack.get_flight_status("KQ100"))
    assert text.startswith("Demonstration data for KQ100")
    assert "Live status" not in text


def test_unknown_demo_flight_is_not_invented():
    results = [aviationstack.get_flight_status("KQ555") for _ in range(5)]
    assert all(r["available"] is False and r["status"] is None for r in results)
    assert "not one of the demonstration flights" in results[0]["reason"]


def test_unavailable_text_points_to_official_source():
    text = aviationstack.describe(aviationstack.get_flight_status("KQ555"))
    assert "couldn't get the current status" in text
    assert "Kenya Airways flight status page" in text


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}  {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
