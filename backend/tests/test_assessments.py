from uuid import UUID, uuid4

import pytest

from event_review.claims import ClaimStore
from event_review.memory import MemoryStore
from event_review.models import CorrectionRequest
from event_review.worker import Worker


EU = UUID("00000001-0000-4000-8000-000000000001")


def feed(client, region="europe"):
    assert client.post("/api/session", json={"account_id": f"supervisor-{region}"}).status_code == 200
    response = client.get("/api/events")
    assert response.status_code == 200
    return response.json()


def test_feed_distinguishes_pending_reviews_model_results_and_supervisor_corrections(settings, admin, client):
    assert all(item["assessment"] is None for item in feed(client)["items"])
    worker = Worker(settings, "europe", "assessment-test")
    for _ in range(4):
        assert worker.run_once()
    reviewed = feed(client)
    assert reviewed["counts"]["safety_events"] == 3
    assessment = next(item["assessment"] for item in reviewed["items"] if item["id"] == str(EU))
    assert assessment == {"decision": "confirmed", "severity": "high", "source": "agent", "model_name": "deterministic-mock-v7"}
    question = next(item for item in reviewed["items"] if item["status"] == "needs_input")
    assert question["assessment"]["decision"] == "needs_input"
    assert question["assessment"]["severity"] is None

    memory = MemoryStore(settings, "europe")
    for version, decision, severity in [(0, "confirmed", "low"), (1, "rejected", None)]:
        memory.correct(EU, "supervisor-europe", CorrectionRequest(
            request_id=uuid4(), expected_version=version, decision=decision,
            severity=severity, reason="Synthetic supervisor evidence for this event."))
    corrected = feed(client)
    latest = next(item["assessment"] for item in corrected["items"] if item["id"] == str(EU))
    assert latest["decision"] == "rejected" and latest["severity"] is None
    assert latest["source"] == "supervisor"
    assert corrected["counts"]["safety_events"] == 2
    # The read projection must not rewrite the original agent's assessment.
    assert client.get(f"/api/events/{EU}").json()["review"]["result"]["severity"] == "high"
    other = feed(client, "us")
    assert other["counts"]["safety_events"] == 0
    assert all(item["assessment"] is None for item in other["items"])


def test_feed_reports_the_saved_provider_and_harness_source(settings, admin, client):
    admin.execute("DELETE FROM review.events WHERE region_id='europe' AND id <> %s", (EU,))
    assert Worker(settings, "europe", "assessment-test").run_once()
    # Exercise persisted provenance without any external model calls.
    admin.execute("UPDATE review.attempts SET model_name='codex/cli-default' WHERE event_id=%s", (EU,))
    assert feed(client)["items"][0]["assessment"]["model_name"] == "codex/cli-default"
    admin.execute("UPDATE review.attempts SET model_output = model_output || '{\"source\":\"harness\"}'::jsonb WHERE event_id=%s", (EU,))
    assert feed(client)["items"][0]["assessment"]["source"] == "harness"


def test_paging_reaches_older_events_without_shifting_on_new_arrivals(settings, admin, client):
    admin.execute("DELETE FROM review.events WHERE region_id='europe' AND id <> %s", (EU,))
    assert Worker(settings, "europe", "assessment-test").run_once()
    admin.execute("""
        INSERT INTO review.events
            (id, region_id, source_ref, event_type, title, description, site, zone, camera, occurred_at, metadata)
        SELECT md5('assessment-window-' || n)::uuid, 'europe', 'assessment-window-' || n, 'test',
               'New arrival', 'Synthetic listing fixture', 'North warehouse', 'Dock 3', 'EU-CAM-04',
               '2026-11-12T12:00:00Z'::timestamptz, '{}' FROM generate_series(1, 105) AS n
    """)
    result = feed(client)
    assert result["counts"]["total"] == 106 and result["counts"]["safety_events"] == 1
    assert len(result["items"]) == 25
    assert all(item["assessment"] is None for item in result["items"])
    assert result["matched"] == 106
    # Every filter runs before the limit, including combinations. The one old
    # confirmed review is invisible on the first page.
    for params in [
        {"category": "safety_events"},
        {"severity": "high"},
        {"event_type": "pedestrian_vehicle_proximity"},
        {"q": "EUROPE-fixture-1"},
        {"category": "safety_events", "severity": "high", "event_type": "pedestrian_vehicle_proximity", "q": "  europe-fixture-1  "},
    ]:
        response = client.get("/api/events", params=params)
        assert response.status_code == 200
        filtered = response.json()
        assert [item["id"] for item in filtered["items"]] == [str(EU)]
        assert filtered["matched"] == 1
        assert filtered["counts"] == result["counts"]
        assert filtered["event_types"] == result["event_types"]

    expected = {str(row[0]) for row in admin.execute("SELECT id FROM review.events WHERE region_id='europe'").fetchall()}
    admin.execute("""
        INSERT INTO review.events
            (id, region_id, source_ref, event_type, title, description, site, zone, camera, occurred_at, metadata)
        VALUES (%s, 'europe', 'newest-arrival', 'test', 'New arrival', 'Pagination fixture',
                'North warehouse', 'Dock 3', 'EU-CAM-04', '2026-11-15T12:00:00Z', '{}')
    """, (uuid4(),))
    seen = [item["id"] for item in result["items"]]
    while result["next_page"]:
        cursor = result["next_page"]
        result = client.get("/api/events", params={"before_at": cursor["occurred_at"], "before_id": cursor["id"]}).json()
        assert result["matched"] == 107
        seen.extend(item["id"] for item in result["items"])
    assert len(seen) == len(set(seen)) == 106
    assert set(seen) == expected and str(EU) in seen
    assert client.get("/api/events").json()["items"][0]["source_ref"] == "newest-arrival"
    feed(client, "us")
    other = client.get("/api/events", params={"before_at": cursor["occurred_at"], "before_id": cursor["id"]}).json()
    assert all(item["region_id"] == "us" for item in other["items"])


def test_filters_follow_latest_correction_and_do_not_restore_model_severity(settings, admin, client):
    worker = Worker(settings, "europe", "filter-test")
    for _ in range(4):
        assert worker.run_once()
    feed(client)
    memory = MemoryStore(settings, "europe")
    memory.correct(EU, "supervisor-europe", CorrectionRequest(
        request_id=uuid4(), expected_version=0, decision="confirmed", severity="low", reason="Low severity in the supplied context."))
    low = client.get("/api/events", params={"category": "safety_events", "severity": "low"}).json()
    assert [item["id"] for item in low["items"]] == [str(EU)]
    memory.correct(EU, "supervisor-europe", CorrectionRequest(
        request_id=uuid4(), expected_version=1, decision="rejected", severity=None, reason="Supervisor verifies this was an incorrect detection."))
    dismissed = client.get("/api/events", params={"category": "dismissed"}).json()
    assert [item["id"] for item in dismissed["items"]] == [str(EU)]
    assert dismissed["matched"] == 1
    assert dismissed["items"][0]["assessment"]["severity"] is None
    safety = client.get("/api/events", params={"category": "safety_events"}).json()
    assert safety["matched"] == 2
    assert str(EU) not in {item["id"] for item in safety["items"]}
    assert client.get("/api/events", params={"category": "dismissed", "severity": "high"}).json()["matched"] == 0
    question = client.get("/api/events", params={"category": "needs_input"}).json()
    assert question["matched"] == 1 and question["items"][0]["status"] == "needs_input"
    assert client.get(f"/api/events/{EU}").json()["review"]["result"]["severity"] == "high"
    feed(client, "us")
    assert client.get("/api/events", params={"category": "dismissed"}).json()["matched"] == 0


def test_work_filters_include_both_pending_states_and_failures(settings, admin, client):
    feed(client)
    assert ClaimStore(settings, "europe").claim_next("filter-test")
    pending = client.get("/api/events", params={"category": "pending"}).json()
    assert pending["matched"] == 4
    assert {item["status"] for item in pending["items"]} == {"available", "claimed"}
    admin.execute("UPDATE review.events SET status='failed' WHERE region_id='europe' AND source_ref='europe-fixture-3'")
    failed = client.get("/api/events", params={"category": "failed"}).json()
    assert failed["matched"] == 1 and failed["items"][0]["source_ref"] == "europe-fixture-3"
    assert client.get("/api/events", params={"category": "pending"}).json()["matched"] == 3


def test_search_is_literal_and_region_scoped_with_stable_type_options(settings, admin, client):
    admin.execute("UPDATE review.events SET title='100%%_ inspected' WHERE id=%s", (EU,))
    initial = feed(client)
    literal = client.get("/api/events", params={"q": "%_"}).json()
    assert [item["id"] for item in literal["items"]] == [str(EU)]
    for params in [{"q": "US-CAM"}, {"q": "' OR true --"}, {"category": "dismissed"}, {"event_type": "unknown"}]:
        empty = client.get("/api/events", params=params).json()
        assert empty["items"] == [] and empty["matched"] == 0
        assert empty["counts"] == initial["counts"]
        assert empty["event_types"] == initial["event_types"]


@pytest.mark.parametrize("params", [
    {"category": "unknown"}, {"severity": "critical"}, {"status": "unknown"},
    {"q": "x" * 201}, {"event_type": "x" * 101},
    {"before_id": str(EU)}, {"before_at": "2026-11-12T12:00:00Z"},
    {"before_id": str(EU), "before_at": "2026-11-12T12:00:00"},
])
def test_invalid_filters_are_rejected(client, params):
    feed(client)
    assert client.get("/api/events", params=params).status_code == 422
