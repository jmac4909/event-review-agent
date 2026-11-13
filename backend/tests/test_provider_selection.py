from dataclasses import replace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import psycopg
import pytest

from event_review.app import create_app
from event_review.claims import ClaimStore
from event_review.database import EventStore
from event_review.feed import FeedConflict, FeedStore, ReviewerUnavailable
from event_review.models import CorrectionRequest, FeedRequest, ModelReply, ReviewerRequest
from event_review.reviewer import MockModel
from event_review.settings import Settings
from event_review.worker import Worker


EXIT = UUID("00000001-0000-4000-8000-000000000003")


def select(feed, mode, version=None):
    return feed.set_reviewer("supervisor-europe", ReviewerRequest(
        reviewer=mode, expected_version=feed.get()["version"] if version is None else version))


def test_selection_is_durable_regional_and_pauses_the_stream(settings, admin):
    settings = replace(settings, model_mode="regional", enabled_models=("anthropic", "codex"))
    feed = FeedStore(settings, "europe")
    feed.set_running("supervisor-europe", FeedRequest(running=True, reviewer="mock", expected_version=0))
    first = feed.ingest_due()
    original = feed.events.inspect_event("europe", first["id"])
    changed = select(feed, "codex")
    assert changed["reviewer"] == "codex" and changed["status"] == "paused"
    assert feed.ingest_due() is None
    assert feed.events.inspect_event("europe", first["id"]) == original
    assert FeedStore(settings, "europe").get() == changed
    assert FeedStore(settings, "us").get()["reviewer"] == "mock"
    with EventStore(settings).for_region("us") as connection:
        assert connection.execute("UPDATE review.event_feed SET selected_reviewer='anthropic' WHERE region_id='europe'").rowcount == 0
    with pytest.raises(FeedConflict, match="reviewer changed"):
        feed.set_running("supervisor-europe", FeedRequest(running=True, reviewer="mock", expected_version=changed["version"]))
    assert feed.set_running("supervisor-europe", FeedRequest(
        running=True, reviewer="codex", expected_version=changed["version"]))["status"] == "running"


def test_selection_lost_acknowledgement_and_stale_replays(settings):
    feed = FeedStore(replace(settings, model_mode="regional", enabled_models=("codex",)), "europe")
    first = select(feed, "codex", 0)
    assert select(feed, "codex", 0) == first
    assert select(feed, "codex")["version"] == first["version"]
    feed.set_running("supervisor-europe", FeedRequest(running=True, reviewer="codex", expected_version=first["version"]))
    with pytest.raises(FeedConflict):
        select(feed, "codex", 0)
    assert feed.get()["status"] == "running"
    select(feed, "mock")
    with pytest.raises(FeedConflict):
        select(feed, "codex", first["version"])
    assert feed.get()["reviewer"] == "mock"


def test_unavailable_selection_cannot_change_controls(settings):
    settings = replace(settings, model_mode="regional")
    feed = FeedStore(settings, "europe")
    before = feed.get()
    assert {p["id"]: p["enabled"] for p in before["providers"]} == {"mock": True, "anthropic": False, "codex": False}
    for mode in ("anthropic", "codex"):
        with pytest.raises(ReviewerUnavailable):
            select(feed, mode)
    assert feed.get() == before


def test_selector_api_derives_region_and_rejects_unavailable_or_stale_choices(settings):
    settings = replace(settings, model_mode="regional", enabled_models=("codex",))
    with TestClient(create_app(settings)) as client:
        body = {"reviewer": "codex", "expected_version": 0}
        assert client.put("/api/reviewer", json=body).status_code == 401
        client.post("/api/session", json={"account_id": "supervisor-us"})
        assert client.put("/api/reviewer", json=body, headers={"Origin": "https://unrelated.example"}).status_code == 403
        assert client.put("/api/reviewer", json=body | {"region": "europe"}).status_code == 422
        assert client.put("/api/reviewer", json=body | {"reviewer": "anthropic"}).status_code == 422
        saved = client.put("/api/reviewer?region=europe", json=body)
        assert saved.status_code == 200 and saved.json()["region_id"] == "us"
        assert saved.json()["updated_by"] == "supervisor-us"
        assert FeedStore(settings, "europe").get()["reviewer"] == "mock"
        assert client.put("/api/reviewer", json=body).json() == saved.json()
        assert client.put("/api/reviewer", json=body | {"reviewer": "mock"}).status_code == 409


def test_running_investigation_keeps_its_provider_after_a_switch(settings, admin, monkeypatch):
    settings = replace(settings, model_mode="regional", enabled_models=("anthropic", "codex"))
    admin.execute("DELETE FROM review.events WHERE region_id='europe' AND id <> %s", (EXIT,))
    feed = FeedStore(settings, "europe")
    calls = []

    class TestProvider(MockModel):
        def __init__(self, mode):
            self.mode, self.name = mode, f"{mode}/injected-test"

        def review(self, context, timeout_seconds=90):
            calls.append(self.mode)
            if self.mode == "anthropic" and not context["tool_results"]:
                select(feed, "codex")  # Another supervisor changes it during the first call.
                return ModelReply({"decision": None, "severity": None, "question": None,
                    "rationale": "Check additional site evidence.",
                    "lookup": {"name": "get_site_context", "event_type": None, "lookback_hours": None}})
            return super().review(context)

    monkeypatch.setattr("event_review.worker.make_model", lambda settings, mode=None: TestProvider(mode or settings.default_reviewer))
    worker = Worker(settings, "europe", "switch-test")
    select(feed, "anthropic")
    assert worker.run_once()
    original = EventStore(settings).inspect_event("europe", EXIT)
    attempt = original["attempts"][0]
    assert attempt["reviewer_mode"] == "anthropic"
    assert [s["name"] for s in attempt["steps"] if s["kind"] == "model"] == ["anthropic/injected-test"] * 2
    assert calls == ["anthropic", "anthropic"]
    with EventStore(settings).for_region("europe") as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("UPDATE review.attempts SET reviewer_mode='codex' WHERE id=%s", (attempt["id"],))
    later = worker.memory.simulate(EXIT, uuid4())
    assert worker.run_once()
    saved = EventStore(settings).inspect_event("europe", later["id"])
    assert saved["attempts"][0]["reviewer_mode"] == "codex"
    assert saved["attempts"][0]["model_name"] == "codex/injected-test"
    assert calls == ["anthropic", "anthropic", "codex"]
    assert EventStore(settings).inspect_event("europe", EXIT)["review"] == original["review"]


def test_new_retry_claim_uses_current_selection_but_old_attempt_keeps_its_mode(settings):
    settings = replace(settings, model_mode="regional", enabled_models=("codex",))
    claims = ClaimStore(settings, "europe")
    first = claims.claim_next("test-worker")
    claims.fail(first["event_id"], first["id"], "test-worker", "Test failure")
    select(FeedStore(settings, "europe"), "codex")
    retry = claims.claim_next("test-worker")
    assert retry["event_id"] == first["event_id"] and retry["reviewer_mode"] == "codex"
    attempts = EventStore(settings).inspect_event("europe", first["event_id"])["attempts"]
    assert next(a for a in attempts if a["id"] == str(first["id"]))["reviewer_mode"] == "mock"


def test_draft_claim_keeps_its_provider_and_next_draft_uses_new_selection(settings, admin, monkeypatch):
    settings = replace(settings, model_mode="regional", enabled_models=("anthropic", "codex"))
    admin.execute("DELETE FROM review.events WHERE region_id='europe' AND id <> %s", (EXIT,))
    feed = FeedStore(settings, "europe")
    modes = []

    class TestProvider(MockModel):
        def __init__(self, mode):
            self.mode, self.name = mode, f"{mode}/injected-test"

        def draft(self, context, timeout_seconds=90):
            modes.append(self.mode)
            if self.mode == "anthropic":
                select(feed, "codex")
            return super().draft(context)

    monkeypatch.setattr("event_review.worker.make_model", lambda settings, mode=None: TestProvider(mode or settings.default_reviewer))
    worker = Worker(settings, "europe", "draft-switch-test")
    worker.run_once()
    for version, severity in ((0, "high"), (1, "low")):
        worker.memory.correct(EXIT, "supervisor-europe", CorrectionRequest(
            request_id=uuid4(), expected_version=version, decision="confirmed", severity=severity,
            reason=f"Site policy: Matching exit obstructions have {severity} severity. This is a standing rule."))
        if version == 0:
            select(feed, "anthropic")
        assert worker.draft_once()
    rows = worker.memory.list_memory()["guidance"]
    assert modes == ["anthropic", "codex"]
    assert [g["draft_history"][0]["reviewer_mode"] for g in rows] == ["codex", "anthropic"]
    assert [g["draft_history"][0]["model_name"] for g in rows] == ["codex/injected-test", "anthropic/injected-test"]


def test_disabled_provider_waits_without_spending_attempts_or_falling_back(settings, admin):
    settings = replace(settings, model_mode="regional")
    admin.execute("DELETE FROM review.events WHERE region_id='europe' AND id <> %s", (EXIT,))
    worker = Worker(settings, "europe", "disabled-test")
    worker.run_once()
    worker.memory.correct(EXIT, "supervisor-europe", CorrectionRequest(
        request_id=uuid4(), expected_version=0, decision="confirmed", severity="high", reason="Site policy: High severity."))
    later = worker.memory.simulate(EXIT, uuid4())
    select(FeedStore(replace(settings, model_mode="regional", enabled_models=("codex",)), "europe"), "codex")
    assert worker.feed.get()["status"] == "reviewer_unavailable"
    assert worker.run_once() is False and worker.draft_once() is False
    assert EventStore(settings).inspect_event("europe", later["id"])["attempts"] == []
    assert worker.memory.list_memory()["guidance"][0]["draft_history"] == []
    select(worker.feed, "mock")
    assert worker.run_once() and worker.draft_once()


def test_configured_provider_setup_failure_is_visible_without_mock_fallback(settings, admin, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = replace(settings, model_mode="regional", enabled_models=("anthropic",))
    admin.execute("DELETE FROM review.events WHERE region_id='europe' AND id <> %s", (EXIT,))
    select(FeedStore(settings, "europe"), "anthropic")
    worker = Worker(settings, "europe", "unconfigured-test")
    worker.run_once()
    saved = EventStore(settings).inspect_event("europe", EXIT)
    assert saved["review"] is None
    assert saved["attempts"][0]["reviewer_mode"] == "anthropic"
    assert "not configured" in saved["attempts"][0]["error"]


def test_enabled_provider_configuration_is_explicit(monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL_MODE", "regional")
    monkeypatch.setenv("REVIEW_ENABLED_MODELS", "codex, anthropic")
    assert Settings.from_env().available_models == ("mock", "anthropic", "codex")
    for mode in ("mock", "anthropic", "codex"):
        monkeypatch.setenv("REVIEW_MODEL_MODE", mode)
        assert Settings.from_env().available_models == (mode,)
    monkeypatch.setenv("REVIEW_ENABLED_MODELS", "unknown")
    with pytest.raises(ValueError, match="REVIEW_ENABLED_MODELS"):
        Settings.from_env()


@pytest.mark.parametrize("mode", ["mock", "anthropic", "codex"])
def test_global_mode_overrides_saved_choices_for_api_stream_reviews_and_drafts(settings, admin, monkeypatch, mode):
    # Provider transports are injected; real PostgreSQL, API, claims and harness.
    # A stale saved choice must never turn global Mock into a paid call.
    monkeypatch.setenv("REVIEW_MODEL_MODE", mode)
    settings = replace(settings, model_mode=Settings.from_env().model_mode,
                       enabled_models=("anthropic", "codex"))
    saved_choice = "anthropic" if mode == "codex" else "codex"
    admin.execute("UPDATE review.event_feed SET selected_reviewer=%s", (saved_choice,))
    admin.execute("DELETE FROM review.events WHERE event_type <> 'blocked_exit'")
    calls = []

    class TestProvider(MockModel):
        def __init__(self, selected):
            self.selected, self.name = selected, f"{selected}/injected-test"

        def review(self, context, timeout_seconds=90):
            calls.append((self.selected, "review", context["event"]["region_id"]))
            return super().review(context)

        def draft(self, context, timeout_seconds=90):
            calls.append((self.selected, "draft", context["event"]["region_id"]))
            return super().draft(context)

    monkeypatch.setattr("event_review.worker.make_model", lambda config, mode=None: TestProvider(mode or config.default_reviewer))
    with TestClient(create_app(settings)) as client:
        for index, region in enumerate(("europe", "us", "asia"), 1):
            worker = Worker(settings, region, f"global-{region}")
            source = UUID(f"0000000{index}-0000-4000-8000-000000000003")
            client.post("/api/session", json={"account_id": f"supervisor-{region}"})
            before = client.get("/api/feed").json()
            assert before["reviewer"] == mode and before["model_selection"] == "global"
            for attempted in (saved_choice, mode):
                response = client.put("/api/reviewer", json={"reviewer": attempted, "expected_version": before["version"]})
                assert response.status_code == 422 and "all regions" in response.json()["detail"]
            assert client.get("/api/feed").json() == before
            assert worker.run_once()
            original = worker.claims.events.inspect_event(region, source)
            assert original["attempts"][0]["reviewer_mode"] == mode
            assert original["attempts"][0]["model_name"] == f"{mode}/injected-test"
            worker.memory.correct(source, f"supervisor-{region}", CorrectionRequest(
                request_id=uuid4(), expected_version=0, decision="confirmed", severity="high",
                reason="Site policy: Matching blocked exits have high severity. This is a standing rule."))
            assert worker.draft_once()
            draft = worker.memory.list_memory()["guidance"][0]["draft_history"][0]
            assert draft["reviewer_mode"] == mode and draft["model_name"] == f"{mode}/injected-test"
            start = client.put("/api/feed", json={"running": True, "reviewer": mode,
                                                 "expected_version": before["version"]})
            assert start.status_code == 200 and start.json()["reviewer"] == mode
            event = worker.feed.ingest_due()
            assert event is not None
            stop = client.put("/api/feed", json={"running": False, "reviewer": mode,
                                                "expected_version": start.json()["version"]})
            assert stop.status_code == 200
            assert worker.run_once()
            assert worker.claims.events.inspect_event(region, event["id"])["attempts"][0]["reviewer_mode"] == mode
            assert worker.claims.events.inspect_event(region, source)["review"] == original["review"]
            # Opting back into regional mode restores the saved preference.
            regional = FeedStore(replace(settings, model_mode="regional"), region).get()
            assert regional["reviewer"] == saved_choice and regional["model_selection"] == "regional"
    assert {selected for selected, _, _ in calls} == {mode}
    assert len(calls) == 9  # Two reviews and one draft in each region.


def test_global_change_stops_old_stream_until_restarted(settings, admin):
    admin.execute("UPDATE review.event_feed SET selected_reviewer='codex', model_mode='codex', enabled=true WHERE region_id='europe'")
    feed = FeedStore(settings, "europe")  # Global Mock ignores the saved Codex preference.
    assert feed.get()["reviewer"] == "mock" and feed.get()["status"] == "reviewer_changed"
    assert feed.ingest_due() is None
    assert admin.execute("SELECT enabled, generated, selected_reviewer FROM review.event_feed WHERE region_id='europe'").fetchone() == (False, 0, "codex")


def test_regional_default_constructs_mock_without_real_provider_setup():
    from event_review.providers import make_model

    assert isinstance(make_model(Settings(model_mode="regional", enabled_models=("anthropic", "codex"))), MockModel)
