from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from event_review.database import EventStore
from event_review.memory import MemoryStore
from event_review.models import CorrectionRequest, GuidanceApproval
from event_review.simulation import scenario_evidence
from event_review.worker import Worker


PROXIMITY = UUID("00000001-0000-4000-8000-000000000001")
ACCESS = UUID("00000001-0000-4000-8000-000000000002")
FOREIGN = UUID("00000002-0000-4000-8000-000000000001")


@pytest.mark.parametrize("source,scenario,distance,day", [
    (PROXIMITY, "repeat", 1.4, None),
    (PROXIMITY, "distance_3m", 3, None),
    (PROXIMITY, "distance_4_99m", 4.99, None),
    (PROXIMITY, "distance_5m", 5, None),
    (ACCESS, "november_12", None, 12),
    (ACCESS, "november_14", None, 14),
    (ACCESS, "same_day", None, 0),
    (ACCESS, "two_days_later", None, 2),
])
def test_preset_changes_evidence_only_and_survives_replay(settings, admin, client, source, scenario, distance, day):
    worker = Worker(settings, "europe", "scenario-test")
    while worker.run_once():
        pass
    store = EventStore(settings)
    original = store.inspect_event("europe", source)
    client.post("/api/session", json={"account_id": "supervisor-europe"})
    request_id = str(uuid4())
    body = {"request_id": request_id, "scenario": scenario}
    url = f"/api/events/{source}/simulate"
    before = admin.execute("SELECT clock_timestamp()").fetchone()[0]
    response = client.post(url, json=body)
    assert response.status_code == 200
    created = response.json()
    after = admin.execute("SELECT clock_timestamp()").fetchone()[0]
    assert created["id"] == request_id and created["status"] == "available"
    assert created["simulation_scenario"] == scenario
    for field in ("region_id", "event_type", "site", "zone", "camera"):
        assert created[field] == original[field]
    expected_metadata = dict(original["metadata"])
    if distance is not None:
        expected_metadata["estimated_distance_m"] = distance
    assert created["metadata"] == expected_metadata
    timestamp = datetime.fromisoformat(created["occurred_at"])
    if scenario in {"same_day", "two_days_later"}:
        assert timestamp == original["occurred_at"] + timedelta(days=day)
    elif day is not None:
        assert timestamp == datetime(2026, 11, day, 12, tzinfo=timezone.utc)
    else:
        assert before <= timestamp <= after
    if scenario.startswith("distance_"):
        assert f"{distance} m separation" in created["description"]
    assert client.post(url, json=body).json() == created
    assert worker.run_once()
    saved = store.inspect_event("europe", UUID(request_id))
    assert saved["review"] is not None
    context = saved["attempts"][0]["context"]
    assert context["event"]["metadata"] == expected_metadata
    assert "simulation_scenario" not in context["event"]
    assert context["memory"]["approved_guidance"] == []
    assert client.post(url, json=body).json()["status"] == saved["status"]
    assert store.inspect_event("europe", source) == original
    assert worker.memory.list_memory() == {"corrections": [], "guidance": []}
    listed = client.get("/api/events").json()["items"]
    assert next(row for row in listed if row["id"] == request_id)["simulation_scenario"] == scenario


@pytest.mark.parametrize("change", [
    {"scenario": "unknown"}, {"scenario": None}, {"scenario": "november_12"},
    {"scenario": "same_day"}, {"scenario": "two_days_later"},
    {"scenario": "distance_3m", "region_id": "us"},
    {"scenario": "distance_3m", "metadata": {"estimated_distance_m": 6}},
    {"scenario": "distance_3m", "decision": "rejected"},
])
def test_invalid_scenarios_do_not_write(client, admin, change):
    client.post("/api/session", json={"account_id": "supervisor-europe"})
    before = admin.execute("SELECT count(*) FROM review.events").fetchone()[0]
    response = client.post(f"/api/events/{PROXIMITY}/simulate", json={"request_id": str(uuid4())} | change)
    assert response.status_code == 422
    assert admin.execute("SELECT count(*) FROM review.events").fetchone()[0] == before
    assert client.post(f"/api/events/{ACCESS}/simulate", json={"request_id": str(uuid4()), "scenario": "distance_5m"}).status_code == 422


def test_concurrent_replay_and_changed_scenario_are_distinct(settings, client, admin):
    memory = MemoryStore(settings, "europe")
    request_id, ready = uuid4(), Barrier(2)
    def create(_):
        ready.wait(timeout=5)
        return memory.simulate(PROXIMITY, request_id, "distance_3m")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert results[0] == results[1]
    assert admin.execute("SELECT count(*) FROM review.events WHERE id=%s", (request_id,)).fetchone()[0] == 1
    client.post("/api/session", json={"account_id": "supervisor-europe"})
    for source, scenario in [(PROXIMITY, "distance_5m"), (PROXIMITY, "repeat"), (ACCESS, "november_12")]:
        assert client.post(f"/api/events/{source}/simulate", json={"request_id": str(request_id), "scenario": scenario}).status_code == 409
    assert client.post(f"/api/events/{FOREIGN}/simulate", json={"request_id": str(uuid4()), "scenario": "distance_3m"}).status_code == 404
    assert client.post(f"/api/events/{PROXIMITY}/simulate", json={"request_id": str(uuid4()), "scenario": "distance_3m"},
                       headers={"X-Expected-Supervisor": "supervisor-us"}).status_code == 412


def test_legacy_simulation_replays_without_backfilling_history(settings, admin):
    memory = MemoryStore(settings, "europe")
    request_id = uuid4()
    memory.simulate(PROXIMITY, request_id)
    admin.execute("UPDATE review.events SET simulation_scenario=NULL WHERE id=%s", (request_id,))
    assert memory.simulate(PROXIMITY, request_id)["simulation_scenario"] is None


@pytest.mark.parametrize("timestamp", [
    datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc),
    datetime(2028, 2, 28, 9, 30, tzinfo=timezone.utc),
])
def test_relative_date_scenarios_cross_month_and_year_boundaries(timestamp):
    source = dict(event_type="restricted_area_entry", occurred_at=timestamp, metadata={}, description="Entry")
    assert scenario_evidence(source, "same_day")["occurred_at"] == timestamp
    assert scenario_evidence(source, "two_days_later")["occurred_at"] == timestamp + timedelta(days=2)


def test_changed_distance_receives_guidance_but_mock_does_not_pretend_to_interpret_it(settings):
    worker = Worker(settings, "europe", "scenario-test")
    while worker.run_once():
        pass
    source = worker.memory.simulate(PROXIMITY, uuid4(), "distance_3m")
    assert worker.run_once()
    original = worker.claims.events.inspect_event("europe", source["id"])
    assert original["review"]["result"]["decision"] == "rejected"
    wording = "At this camera, separations below 5 metres are high-severity alerts. At least 5 metres is allowed."
    worker.memory.correct(source["id"], "supervisor-europe", CorrectionRequest(
        request_id=uuid4(), expected_version=0, decision="confirmed", severity="high", reason=wording))
    guidance = worker.memory.list_memory()["guidance"][0]
    worker.memory.approve(guidance["id"], "supervisor-europe", GuidanceApproval(text=wording))
    for scenario in ("distance_4_99m", "distance_5m"):
        later = worker.memory.simulate(source["id"], uuid4(), scenario)
        assert worker.run_once()
        saved = worker.claims.events.inspect_event("europe", later["id"])
        assert saved["review"]["result"]["decision"] == "needs_input"
        assert saved["attempts"][0]["context"]["memory"]["approved_guidance"][0]["id"] == str(guidance["id"])
    assert worker.claims.events.inspect_event("europe", source["id"])["review"] == original["review"]
