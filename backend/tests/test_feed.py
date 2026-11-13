from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import psycopg
import pytest

from event_review.bootstrap import bootstrap
from event_review.database import EventStore
from event_review.feed import FeedConflict, FeedStore, MAX_UNRESOLVED
from event_review.models import FeedRequest
from event_review.worker import Worker


@pytest.fixture
def feed(settings):
    return FeedStore(settings, "europe")


def control(feed, running=True, version=None, reviewer=None):
    return feed.set_running(f"supervisor-{feed.region}", FeedRequest(
        running=running, expected_version=feed.get()["version"] if version is None else version,
        reviewer=reviewer or feed.settings.model_mode,
    ))


def due(admin, region="europe"):
    admin.execute("UPDATE review.event_feed SET next_event_at = clock_timestamp() - interval '1 day' WHERE region_id = %s", (region,))


def test_source_starts_paused_and_preserves_controls_across_setup_and_restart(settings, feed):
    assert feed.get()["status"] == "paused"
    assert feed.ingest_due() is None
    started = control(feed)
    assert started["status"] == "running" and started["version"] == 1
    event = feed.ingest_due()
    assert event["region_id"] == "europe" and event["source_ref"] == "demo-stream-1"
    bootstrap(settings.pg_host, settings.pg_port, settings.pg_database)
    restarted = FeedStore(settings, "europe")
    saved = restarted.get()
    assert saved["status"] == "running" and saved["generated"] == 1
    assert saved["last_event_id"] == event["id"]
    assert saved["updated_by"] == "supervisor-europe"
    assert restarted.ingest_due() is None


def test_two_producers_create_only_one_due_event(feed):
    control(feed)
    ready = Barrier(2)

    def produce(_):
        ready.wait(timeout=5)
        return FeedStore(feed.settings, "europe").ingest_due()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(produce, range(2)))
    assert sum(event is not None for event in results) == 1
    assert feed.get()["generated"] == 1


def test_source_recovers_without_replaying_missed_intervals(feed, admin):
    control(feed)
    due(admin)
    assert feed.get()["status"] == "waiting_for_worker"
    event = feed.ingest_due()
    state = admin.execute("SELECT generated, next_event_at FROM review.event_feed WHERE region_id='europe'").fetchone()
    assert state[0] == 1
    assert (state[1] - event["occurred_at"]).total_seconds() == feed.settings.feed_interval_seconds
    assert feed.ingest_due() is None


def test_pause_and_resume_are_durable_without_erasing_existing_events(feed, admin):
    control(feed)
    first = feed.ingest_due()
    original = feed.events.get_event("europe", first["id"])
    paused = control(feed, False)
    assert paused["status"] == "paused"
    due(admin)
    assert FeedStore(feed.settings, "europe").ingest_due() is None
    control(feed)
    second = feed.ingest_due()
    assert second["id"] != first["id"] and second["source_ref"] == "demo-stream-2"
    assert feed.events.get_event("europe", first["id"]) == original


def test_event_and_source_cursor_roll_back_together_on_failure(feed, admin):
    control(feed)
    admin.execute("""
        CREATE FUNCTION review.fail_test_feed_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Synthetic source failure'; END $$;
        CREATE TRIGGER fail_test_feed_update BEFORE UPDATE OF generated ON review.event_feed
        FOR EACH ROW EXECUTE FUNCTION review.fail_test_feed_update();
    """)
    with pytest.raises(psycopg.errors.RaiseException, match="Synthetic source failure"):
        feed.ingest_due()
    assert feed.get()["generated"] == 0
    assert admin.execute("SELECT count(*) FROM review.events WHERE region_id='europe'").fetchone()[0] == 4
    admin.execute("DROP TRIGGER fail_test_feed_update ON review.event_feed")
    assert feed.ingest_due()["source_ref"] == "demo-stream-1"


def test_backpressure_waits_for_unresolved_reviews_then_resumes(settings, feed, admin):
    # Existing human questions count too, not just the worker's available queue.
    worker = Worker(settings, "europe", "test-worker")
    while worker.run_once():
        pass
    assert feed.get()["unresolved"] == 1
    control(feed)
    for _ in range(MAX_UNRESOLVED - 1):
        due(admin)
        assert feed.ingest_due() is not None
    due(admin)
    assert feed.get()["status"] == "waiting_for_reviews"
    assert feed.ingest_due() is None
    assert worker.run_once()  # Reviews a queued proximity event, creating room.
    assert feed.get()["unresolved"] == MAX_UNRESOLVED - 1
    assert feed.ingest_due() is not None
    assert feed.get()["unresolved"] == MAX_UNRESOLVED


def test_control_replay_and_stale_tabs_cannot_reverse_newer_choices(feed):
    first = control(feed, version=0)
    assert control(feed, version=0) == first  # Lost acknowledgement.
    assert control(feed, version=first["version"])["version"] == first["version"]  # Desired state already holds.
    paused = control(feed, False)
    with pytest.raises(FeedConflict, match="controls changed"):
        control(feed, version=0)
    assert feed.get()["version"] == paused["version"] and feed.get()["status"] == "paused"


@pytest.mark.parametrize("mode", ["anthropic", "codex"])
def test_reviewer_change_needs_explicit_restart_and_rejects_a_stale_start(feed, mode):
    control(feed)
    changed = FeedStore(replace(feed.settings, model_mode=mode), "europe")
    assert changed.get()["status"] == "reviewer_changed"
    assert changed.ingest_due() is None
    assert feed.get()["status"] == "paused"  # Returning to the original mode cannot resume it.
    assert feed.ingest_due() is None
    with pytest.raises(FeedConflict, match="reviewer changed"):
        control(changed, reviewer="mock")
    assert control(changed, reviewer=mode)["status"] == "running"
    assert changed.ingest_due() is not None  # No model is invoked by the source.
    # Pausing is safe even from a browser displaying the previous reviewer.
    assert control(changed, False, reviewer="mock")["status"] == "paused"


def test_workers_keep_reviewing_new_arrivals_and_stop_generating_when_paused(settings, feed, admin):
    admin.execute("DELETE FROM review.events WHERE region_id = 'europe'")
    control(feed)
    worker = Worker(settings, "europe", "first-worker")
    assert worker.run_once()
    first = feed.get()["last_event_id"]
    assert EventStore(settings).inspect_event("europe", first)["review"]["result"]["decision"] == "confirmed"
    due(admin)
    assert Worker(settings, "europe", "restarted-worker").run_once()
    second = feed.get()["last_event_id"]
    assert first != second
    assert EventStore(settings).inspect_event("europe", second)["status"] == "needs_input"
    control(feed, False)
    due(admin)
    assert worker.run_once() is False
    assert feed.get()["generated"] == 2


def test_feed_rls_and_scoped_controls_keep_regions_separate(settings, feed, admin):
    control(feed)
    feed.ingest_due()
    for region in ("us", "asia"):
        other = FeedStore(settings, region)
        assert other.get()["status"] == "paused" and other.get()["generated"] == 0
        assert other.ingest_due() is None
    with EventStore(settings).for_region("us") as connection:
        assert [row["region_id"] for row in connection.execute("SELECT region_id FROM review.event_feed")] == ["us"]
        assert connection.execute("UPDATE review.event_feed SET enabled = false WHERE region_id='europe'").rowcount == 0
    with EventStore(settings).connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM review.event_feed").fetchone()["n"] == 0
    assert admin.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid='review.event_feed'::regclass").fetchone() == (True, True)
    assert feed.get()["status"] == "running"
    with EventStore(settings).for_region("europe") as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute("""
                UPDATE review.event_feed SET last_event_id = '00000002-0000-4000-8000-000000000001'
                WHERE region_id = 'europe'
            """)


def test_feed_api_requires_a_session_and_derives_region(client, settings):
    body = {"running": True, "expected_version": 0, "reviewer": "mock"}
    assert client.get("/api/feed").status_code == 401
    assert client.put("/api/feed", json=body).status_code == 401
    client.post("/api/session", json={"account_id": "supervisor-us"})
    assert client.put("/api/feed", json=body, headers={"Origin": "https://unrelated.example"}).status_code == 403
    response = client.put("/api/feed?region=europe", json=body)
    assert response.status_code == 200 and response.json()["region_id"] == "us"
    assert response.json()["updated_by"] == "supervisor-us"
    assert FeedStore(settings, "europe").get()["status"] == "paused"
    assert client.put("/api/feed", json=body).json()["version"] == 1


def test_older_questions_remain_findable_after_the_latest_event_window_fills(settings, client, admin):
    question_id = "00000001-0000-4000-8000-000000000002"
    admin.execute("DELETE FROM review.events WHERE region_id='europe' AND id <> %s", (question_id,))
    assert Worker(settings, "europe", "question-fixture").run_once()
    # More than one page of newer available events, without changing the old
    # question or needing 105 model calls just to exercise the listing query.
    admin.execute("""
        INSERT INTO review.events
            (id, region_id, source_ref, event_type, title, description, site, zone, camera, occurred_at, metadata)
        SELECT md5('window-event-' || n)::uuid, 'europe', 'window-event-' || n, 'test',
               'New arrival', 'Synthetic listing fixture', 'North warehouse', 'Dock 3', 'EU-CAM-04',
               clock_timestamp(), '{}' FROM generate_series(1, 105) AS n
    """)
    client.post("/api/session", json={"account_id": "supervisor-europe"})
    page = client.get("/api/events").json()
    assert len(page["items"]) == 25 and page["counts"]["total"] == 106
    assert page["counts"]["available"] == 105 and page["counts"]["needs_input"] == 1
    assert question_id not in {item["id"] for item in page["items"]}
    questions = client.get("/api/events?status=needs_input").json()
    assert [item["id"] for item in questions["items"]] == [question_id]
    assert questions["counts"] == page["counts"]
    assert client.get(f"/api/events/{question_id}").json()["review"]["result"]["question"]
    assert client.get("/api/events?status=unrecognized").status_code == 422


@pytest.mark.parametrize("change", [{"running": "true"}, {"region_id": "us"}, {"reviewer": "invented"}, {"expected_version": "0"}])
def test_feed_api_rejects_invalid_controls_without_changes(client, change):
    client.post("/api/session", json={"account_id": "supervisor-europe"})
    response = client.put("/api/feed", json={"running": True, "expected_version": 0, "reviewer": "mock"} | change)
    assert response.status_code == 422
    assert client.get("/api/feed").json()["version"] == 0


def expire_stream(admin):
    admin.execute("UPDATE review.event_feed SET stops_at = clock_timestamp() - interval '1 second' WHERE region_id='europe'")


def test_finite_run_stops_before_next_arrival_and_workers_finish_queued_reviews(settings, feed, admin):
    started = control(feed)
    assert started["run_seconds"] == 300
    assert 299 <= (started["stops_at"] - started["updated_at"]).total_seconds() <= 300
    event = feed.ingest_due()
    expire_stream(admin)
    # Reads report expiry immediately, even before a worker persists the pause.
    assert feed.get()["status"] == "auto_stopped"
    assert feed.ingest_due() is None
    stopped = feed.get()
    assert stopped["version"] == started["version"] + 1 and stopped["generated"] == 1
    assert stopped["updated_by"] == "auto-stop"
    assert admin.execute("SELECT enabled FROM review.event_feed WHERE region_id='europe'").fetchone() == (False,)
    worker = Worker(settings, "europe", "finish-after-auto-stop")
    while worker.run_once():
        pass
    assert feed.events.inspect_event("europe", event["id"])["review"] is not None
    assert feed.get()["generated"] == 1
    assert FeedStore(settings, "us").get()["status"] == "paused"


def test_replayed_start_keeps_deadline_and_cannot_resume_an_expired_run(feed, admin):
    started = control(feed, version=0)
    assert control(feed, version=0)["stops_at"] == started["stops_at"]
    assert control(feed)["stops_at"] == started["stops_at"]
    expire_stream(admin)
    with pytest.raises(FeedConflict):
        control(feed, version=0)
    feed.ingest_due()
    with pytest.raises(FeedConflict):
        control(feed, version=started["version"])
    restarted = control(feed)
    assert restarted["status"] == "running" and restarted["stops_at"] > started["stops_at"]


def test_deadline_stops_when_reviews_are_full_and_two_workers_observe_expiry(feed, admin):
    control(feed)
    for _ in range(MAX_UNRESOLVED - 4):
        due(admin)
        feed.ingest_due()
    assert feed.get()["status"] == "waiting_for_reviews"
    version = feed.get()["version"]
    expire_stream(admin)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: FeedStore(feed.settings, "europe").ingest_due(), range(2))) == [None, None]
    assert feed.get()["status"] == "auto_stopped" and feed.get()["version"] == version + 1


def test_indefinite_and_ten_minute_choices_persist_and_pause_clears_deadline(feed, admin):
    request = FeedRequest(running=True, expected_version=0, reviewer="mock", run_seconds=None)
    state = feed.set_running("supervisor-europe", request)
    assert state["run_seconds"] is None and state["stops_at"] is None
    due(admin)
    assert FeedStore(feed.settings, "europe").ingest_due() is not None
    paused = control(feed, False)
    assert paused["stops_at"] is None
    state = feed.set_running("supervisor-europe", FeedRequest(
        running=True, expected_version=paused["version"], reviewer="mock", run_seconds=600))
    assert 599 <= (state["stops_at"] - state["updated_at"]).total_seconds() <= 600
    assert control(feed, False)["stops_at"] is None


@pytest.mark.parametrize("duration", [0, -1, 30, 301, "300", True])
def test_api_rejects_unsupported_run_lengths(client, duration):
    client.post("/api/session", json={"account_id": "supervisor-europe"})
    response = client.put("/api/feed", json={"running": True, "expected_version": 0,
                                           "reviewer": "mock", "run_seconds": duration})
    assert response.status_code == 422
    assert client.get("/api/feed").json()["version"] == 0


def test_upgrade_bounds_an_existing_stream_once_without_changing_provider_or_history(settings, admin):
    # Recreate the pre-009 shape only inside this disposable fixture database.
    admin.execute("ALTER TABLE review.event_feed DROP COLUMN run_seconds, DROP COLUMN stops_at")
    admin.execute("DELETE FROM review.schema_migrations WHERE name = '009_feed_deadline.sql'")
    admin.execute("UPDATE review.event_feed SET enabled=true, model_mode='codex', selected_reviewer='codex', version=8 WHERE region_id='europe'")
    before = admin.execute("SELECT id, status, metadata FROM review.events ORDER BY id").fetchall()
    bootstrap(settings.pg_host, settings.pg_port, settings.pg_database)
    row = admin.execute("SELECT enabled, model_mode, selected_reviewer, version, stops_at, updated_at FROM review.event_feed WHERE region_id='europe'").fetchone()
    assert row[:4] == (True, "codex", "codex", 9)
    assert 299 <= (row[4] - row[5]).total_seconds() <= 300
    assert admin.execute("SELECT stops_at FROM review.event_feed WHERE region_id='us'").fetchone() == (None,)
    assert admin.execute("SELECT id, status, metadata FROM review.events ORDER BY id").fetchall() == before
    bootstrap(settings.pg_host, settings.pg_port, settings.pg_database)
    assert admin.execute("SELECT stops_at FROM review.event_feed WHERE region_id='europe'").fetchone() == (row[4],)
