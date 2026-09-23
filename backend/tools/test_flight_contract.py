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

from app import aviationstack  # noqa: E402

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
    assert live["delay_minutes"] == 0          # null delay must not crash
    assert live["gate"] == "TBD"
    assert live["status"] == "unknown"
    assert live["route"] == "Unknown -> Unknown"


def test_live_falls_back_to_scheduled_when_no_estimate():
    record = dict(LIVE_RECORD)
    record["departure"] = {"iata": "NBO", "scheduled": "2026-09-23T08:20:00+00:00"}
    live = aviationstack.normalize_live_flight(record, "KQ100")
    assert live["estimated_departure_utc"] == "2026-09-23T08:20:00+00:00"


def test_unknown_flight_still_satisfies_contract():
    result = aviationstack.get_flight_status("ZZ999")
    assert set(result) == set(aviationstack.FLIGHT_STATUS_KEYS)


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
