from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import UUID

import psycopg
import pytest

from event_review.claims import ClaimConflict, ClaimStore
from event_review.context import model_context
from event_review.database import EventStore
from event_review.investigation import Investigation
from event_review.models import ModelReply, validate_verdict
from event_review.reviewer import MockModel, build_context
from event_review.worker import Worker


EU_EVENT = UUID("00000001-0000-4000-8000-000000000001")
US_EVENT = UUID("00000002-0000-4000-8000-000000000001")


@pytest.fixture
def one_event(admin):
    admin.execute("DELETE FROM review.events WHERE region_id = 'europe' AND id <> %s", (EU_EVENT,))


def sign_in(client, region="europe"):
    assert client.post("/api/session", json={"account_id": f"supervisor-{region}"}).status_code == 200


def test_mock_saves_exact_input_before_call_and_keeps_a_durable_review(settings, admin, one_event):
    seen = []

    class RecordingModel(MockModel):
        def review(self, context, timeout_seconds=90):
            stored, started = admin.execute("SELECT steps -> -1 -> 'input', model_started_at FROM review.attempts").fetchone()
            assert stored == context and started is not None
            assert context["event"]["region_id"] == "europe"
            assert "status" not in context["event"]
            assert context["memory"] == {"corrections": [], "approved_guidance": [], "guidance_overflow": False}
            assert "memory_selection" not in context
            assert context["site_rules"]["source_ref"] == "europe-synthetic-site-rules-v1"
            assert context["site_rules"]["rules"]["pedestrian_vehicle_proximity"]["threshold_m"] == 2
            assert "layout" not in context["site_rules"] and "schedule" not in context["site_rules"]
            assert context["lookups_remaining"] == 3 and context["tool_results"] == []
            seen.append(context)
            return super().review(context)

    worker = Worker(settings, "europe", "test-worker", RecordingModel())
    assert worker.run_once()
    assert not Worker(settings, "europe", "restarted-worker").run_once()
    saved = EventStore(settings).inspect_event("europe", EU_EVENT)
    assert saved["status"] == "reviewed"
    assert saved["review"]["result"]["decision"] == "confirmed"
    audit = saved["attempts"][0]["context"]
    assert audit["memory_selection"]["method"] == "lexical"
    assert model_context(audit) == seen[0] and len(seen) == 1
    assert len(saved["attempts"][0]["steps"]) == 1
    assert saved["attempts"][0]["steps"][0]["input"] == seen[0]
    assert saved["attempts"][0]["model_output"] == saved["review"]["result"]
    admin.execute("UPDATE review.events SET description = 'New source text' WHERE id = %s", (EU_EVENT,))
    admin.execute("UPDATE review.site_context SET content = jsonb_set(content, '{rules,pedestrian_vehicle_proximity,threshold_m}', '9') WHERE region_id = 'europe'")
    assert EventStore(settings).inspect_event("europe", EU_EVENT)["attempts"][0]["context"] == audit


def test_needs_input_releases_claim_and_does_not_retry(settings, admin, one_event):
    admin.execute("UPDATE review.events SET event_type = 'restricted_area_entry' WHERE id = %s", (EU_EVENT,))
    worker = Worker(settings, "europe", "test-worker")
    assert worker.run_once() and not worker.run_once()
    saved = EventStore(settings).inspect_event("europe", EU_EVENT)
    assert saved["status"] == "needs_input" and saved["active_attempt_id"] is None
    assert saved["review"]["result"]["question"]
    assert saved["review"]["result"]["severity"] is None
    assert saved["attempts"][0]["status"] == "completed"
    with pytest.raises(ClaimConflict):
        ClaimStore(settings, "europe").retry_failed(EU_EVENT)


def test_two_workers_review_different_events_concurrently(settings, admin):
    ready = Barrier(2)

    class ConcurrentModel(MockModel):
        def review(self, context, timeout_seconds=90):
            ready.wait(timeout=5)
            return super().review(context)

    workers = [Worker(settings, "europe", name, ConcurrentModel()) for name in ("worker-a", "worker-b")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda worker: worker.run_once(), workers))
    assert results == [True, True]
    rows = admin.execute("SELECT event_id, region_id FROM review.reviews").fetchall()
    assert len(rows) == 2 and len({row[0] for row in rows}) == 2
    assert {row[1] for row in rows} == {"europe"}


def test_invalid_outputs_keep_trace_and_stop_after_three_attempts(settings, admin, one_event):
    class InvalidModel:
        name = "invalid-test-model"

        def review(self, context, timeout_seconds=90):
            return ModelReply({"decision": "invented", "secret_extra": "synthetic test value"})

    worker = Worker(settings, "europe", "test-worker", InvalidModel())
    for _ in range(3):
        assert worker.run_once()
    assert not worker.run_once()
    saved = EventStore(settings).inspect_event("europe", EU_EVENT)
    assert saved["status"] == "failed" and saved["review"] is None
    assert len(saved["attempts"]) == 3
    assert all(attempt["context"] and attempt["model_output"]["decision"] == "invented" for attempt in saved["attempts"])
    assert all("invalid verdict" in attempt["error"] for attempt in saved["attempts"])


def test_model_exception_retains_input_without_persisting_exception_details(settings, admin, one_event):
    class BrokenModel:
        name = "broken-test-model"

        def review(self, context, timeout_seconds=90):
            raise RuntimeError("Do not persist provider credentials or internal error details")

    assert Worker(settings, "europe", "test-worker", BrokenModel()).run_once()
    saved = EventStore(settings).inspect_event("europe", EU_EVENT)
    assert saved["status"] == "available"
    attempt = saved["attempts"][0]
    assert attempt["context"] and attempt["model_output"] is None
    assert attempt["error"] == "Reviewer failed before returning an output."
    assert attempt["steps"][0]["usage"] is None


def test_expiry_during_review_discards_output_and_preserves_replacement(settings, admin, one_event):
    replacement = []

    class LateModel(MockModel):
        def review(self, context, timeout_seconds=90):
            admin.execute("""
                UPDATE review.attempts SET started_at = clock_timestamp() - interval '2 hours',
                    expires_at = clock_timestamp() - interval '1 hour'
            """)
            replacement.append(ClaimStore(settings, "europe").claim_next("replacement-worker"))
            return super().review(context)

    assert Worker(settings, "europe", "old-worker", LateModel()).run_once()
    saved = EventStore(settings).inspect_event("europe", EU_EVENT)
    assert saved["review"] is None and saved["active_attempt_id"] == replacement[0]["id"]
    old = next(attempt for attempt in saved["attempts"] if attempt["worker_id"] == "old-worker")
    assert old["status"] == "expired" and old["context"] and old["model_output"] is None
    assert old["steps"][0]["usage"] is None  # Late metadata cannot bypass claim ownership either.


def test_uncertain_save_does_not_repeat_model_or_release_accepted_review(settings, one_event, monkeypatch):
    calls = []

    class CountingModel(MockModel):
        def review(self, context, timeout_seconds=90):
            calls.append(context)
            return super().review(context)

    worker = Worker(settings, "europe", "test-worker", CountingModel())
    save = worker.claims.complete

    def lose_acknowledgement(*args):
        save(*args)
        raise psycopg.OperationalError("Simulated lost commit acknowledgement")

    monkeypatch.setattr(worker.claims, "complete", lose_acknowledgement)
    with pytest.raises(psycopg.OperationalError):
        worker.run_once()
    assert not worker.run_once()
    assert len(calls) == 1  # Rules arrive upfront; no extra call after the lost save acknowledgement.
    assert EventStore(settings).inspect_event("europe", EU_EVENT)["status"] == "reviewed"


def test_inspection_and_retry_enforce_supervisor_region(settings, client, one_event):
    store = ClaimStore(settings, "europe")
    for _ in range(3):
        attempt = store.claim_next("test-worker")
        store.fail(EU_EVENT, attempt["id"], "test-worker", "Synthetic failure")
    assert client.post(f"/api/events/{EU_EVENT}/retry").status_code == 401
    sign_in(client, "us")
    assert client.get(f"/api/events/{EU_EVENT}").status_code == 404
    assert client.post(f"/api/events/{EU_EVENT}/retry").status_code == 404
    sign_in(client)
    failed = client.get(f"/api/events/{EU_EVENT}").json()
    assert failed["status"] == "failed" and len(failed["attempts"]) == 3
    assert client.post(f"/api/events/{EU_EVENT}/retry", headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.post(f"/api/events/{EU_EVENT}/retry?region=us").status_code == 204
    assert client.post(f"/api/events/{EU_EVENT}/retry").status_code == 409
    Worker(settings, "europe", "new-worker").run_once()
    completed = client.get(f"/api/events/{EU_EVENT}").json()
    assert completed["status"] == "reviewed" and len(completed["attempts"]) == 4
    assert completed["review"]["result"]["decision"] == "confirmed"
    assert completed["attempts"][0]["context"]["event"]["region_id"] == "europe"
    assert client.get(f"/api/events/{US_EVENT}").status_code == 404


@pytest.mark.parametrize("changes", [
    {"decision": "other"}, {"severity": None}, {"rationale": " "},
    {"question": "Unneeded question"}, {"extra": True},
    {"decision": "needs_input", "severity": None},
    {"decision": "rejected", "severity": "high"},
])
def test_invalid_verdict_combinations_are_rejected(changes):
    result = {"decision": "confirmed", "severity": "high", "rationale": "Test evidence", "question": None}
    with pytest.raises(ValueError):
        validate_verdict(result | changes)


def test_timeout_consumes_attempts_and_retains_the_input(settings, one_event):
    class TimedOutModel:
        name = "timed-out-model"

        def review(self, context, timeout_seconds=90):
            assert 0 < timeout_seconds <= 0.5
            raise TimeoutError()

    worker = Worker(replace(settings, model_timeout_seconds=0.5), "europe", "timeout-worker", TimedOutModel())
    for _ in range(3):
        assert worker.run_once()
    assert worker.run_once() is False
    saved = EventStore(settings).inspect_event("europe", EU_EVENT)
    assert saved["status"] == "failed" and saved["review"] is None
    assert len(saved["attempts"]) == 3
    assert all(item["context"] and item["error"] == "Model call timed out." for item in saved["attempts"])


def test_response_after_call_deadline_is_not_accepted(settings, one_event, monkeypatch):
    clock = [1000.0]
    calls = []
    monkeypatch.setattr("event_review.worker.monotonic", lambda: clock[0])

    class LateResponse(MockModel):
        def review(self, context, timeout_seconds=90):
            calls.append(context)
            clock[0] += 1  # Deadline passes after the call starts, not during SQL setup.
            return super().review(context)

    worker = Worker(replace(settings, model_timeout_seconds=0.05), "europe", "slow-worker", LateResponse())
    assert worker.run_once()
    saved = EventStore(settings).inspect_event("europe", EU_EVENT)
    assert saved["review"] is None
    assert len(calls) == 1
    assert saved["attempts"][0]["error"] == "Model call timed out."
    # The returned reply still tells us this mock call used no model tokens,
    # even though its late verdict must not be accepted.
    assert saved["attempts"][0]["steps"][0]["usage"]["provider"] == "mock"


def test_mock_rejects_a_non_matching_event_and_requests_missing_evidence(settings, one_event):
    event = EventStore(settings).get_event("europe", EU_EVENT)
    context = build_context(event, site_rules=Investigation(settings, "europe").site_rules(event))
    context["event"]["metadata"]["estimated_distance_m"] = 5
    assert validate_verdict(MockModel().review(context).output).decision == "rejected"
    context["event"]["metadata"].pop("estimated_distance_m")
    assert validate_verdict(MockModel().review(context).output).decision == "needs_input"


@pytest.mark.parametrize("missing_policy", ["different_site", "future_only"])
def test_missing_applicable_rules_ask_for_input_without_a_lookup(settings, admin, one_event, missing_policy):
    if missing_policy == "different_site":
        admin.execute("UPDATE review.events SET site = 'Unconfigured site' WHERE id = %s", (EU_EVENT,))
    else:
        admin.execute("UPDATE review.site_context SET valid_from = valid_from + interval '1 month' WHERE region_id = 'europe'")
    worker = Worker(settings, "europe", "test-worker")
    assert worker.run_once() and not worker.run_once()
    saved = EventStore(settings).inspect_event("europe", EU_EVENT)
    assert saved["status"] == "needs_input"
    assert saved["review"]["result"]["question"] == "Which site rules apply?"
    attempt = saved["attempts"][0]
    assert attempt["context"]["site_rules"] is None
    assert len(attempt["steps"]) == 1 and attempt["steps"][0]["kind"] == "model"
