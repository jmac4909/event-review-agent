from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import UUID, uuid4

import psycopg
import pytest

from event_review.claims import ClaimConflict
from event_review.database import EventNotFound, EventStore
from event_review.drafts import DraftStore
from event_review.memory import FeedbackConflict
from event_review.models import CorrectionRequest, GuidanceApproval, GuidanceDraft, ModelReply
from event_review.reviewer import MockModel
from event_review.worker import Worker


EU = UUID("00000001-0000-4000-8000-000000000001")
REASON = "A fixed barrier separates this walkway from the vehicle route."
OUTPUT = {"reusable": True, "text": "Check the verified barrier before applying this exception.",
          "rationale": "The supervisor described a lasting site condition."}


@pytest.fixture
def pending(settings, admin):
    admin.execute("DELETE FROM review.events WHERE region_id = 'europe' AND id <> %s", (EU,))
    worker = Worker(settings, "europe", "draft-test")
    worker.run_once()
    memory = worker.memory
    memory.correct(EU, "supervisor-europe", CorrectionRequest(request_id=uuid4(), expected_version=0,
                   decision="rejected", severity=None, reason=REASON))
    return worker, memory.list_memory()["guidance"][0]


def start(store, owner="draft-test"):
    claim = store.claim_next(owner)
    store.start(claim["id"], claim["draft_attempt_id"], owner, {"instructions": "Test input"}, "test-model")
    return claim


def finish(store, claim, output=OUTPUT, **kwargs):
    return store.finish(claim["id"], claim["draft_attempt_id"], claim["draft_worker_id"], output=output, **kwargs)


def expire(admin, guidance_id):
    admin.execute("UPDATE review.guidance SET draft_expires_at = clock_timestamp() - interval '1 second' WHERE id = %s", (guidance_id,))


def test_draft_trace_approval_and_later_outcome_preserve_source(settings, pending):
    worker, suggestion = pending
    original = EventStore(settings).inspect_event("europe", EU)["review"]
    assert worker.draft_once()
    drafted = worker.memory.list_memory()["guidance"][0]
    assert drafted["draft_status"] == "completed" and drafted["status"] == "proposed"
    assert drafted["draft_result"]["reusable"] is True
    assert drafted["proposed_text"] == REASON
    attempt = drafted["draft_history"][0]
    assert attempt["context"]["correction"]["reason"] == REASON
    assert attempt["context"]["original_review"] == original["result"]
    assert attempt["context"]["memory"]["selection"]
    assert "selection" not in attempt["model_input"]["memory"]
    assert attempt["model_input"]["correction"] == attempt["context"]["correction"]
    assert attempt["output"] == drafted["draft_result"]
    assert attempt["usage"] == {"provider": "mock", "input_tokens": 0, "output_tokens": 0}
    assert attempt["model_started_at"] <= attempt["model_finished_at"]
    later = worker.memory.simulate(EU, uuid4())
    assert worker.memory.select_for(later)["approved_guidance"] == []
    assert not worker.draft_once()
    worker.memory.approve(suggestion["id"], "supervisor-europe", GuidanceApproval(
        text=drafted["draft_result"]["text"]))
    worker.run_once()
    saved = EventStore(settings).inspect_event("europe", later["id"])
    assert saved["review"]["result"]["decision"] == "needs_input"
    assert "Mock cannot interpret" in saved["review"]["result"]["rationale"]
    assert EventStore(settings).inspect_event("europe", EU)["review"] == original


def test_draft_records_exact_compact_input_before_call(pending, admin):
    worker, _ = pending

    class RecordingModel(MockModel):
        def draft(self, context, timeout_seconds=90):
            stored, audit = admin.execute("SELECT draft_history -> -1 -> 'model_input', draft_history -> -1 -> 'context' FROM review.guidance").fetchone()
            assert stored == context
            assert "selection" not in context["memory"] and audit["memory"]["selection"]
            return super().draft(context, timeout_seconds)

    worker.model = RecordingModel()
    assert worker.draft_once()


def test_one_persons_permit_is_not_a_reusable_mock_rule(settings, admin, pending):
    worker, suggestion = pending
    # Replace the immutable source with a new real correction on an access event.
    access = UUID("00000002-0000-4000-8000-000000000002")
    admin.execute("DELETE FROM review.events WHERE region_id = 'us' AND id <> %s", (access,))
    us = Worker(settings, "us", "us-draft")
    us.run_once()
    us.memory.correct(access, "supervisor-us", CorrectionRequest(request_id=uuid4(), expected_version=0,
                      decision="rejected", severity=None, reason="This person had a permit today."))
    us.draft_once()
    row = us.memory.list_memory()["guidance"][0]
    assert row["draft_result"]["reusable"] is False and row["draft_result"]["text"] is None
    assert row["status"] == "proposed"
    context = row["draft_history"][0]["context"]
    assert context["event"]["region_id"] == "us" and REASON not in str(context)
    later = us.memory.simulate(access, uuid4())
    us.run_once()
    result = EventStore(settings).inspect_event("us", later["id"])["review"]["result"]
    assert result["decision"] == "needs_input" and result["question"]


@pytest.mark.parametrize("source_id,baseline,severity,reason", [
    ("00000001-0000-4000-8000-000000000003", "medium", "high",
     "Site policy: Matching 95-second pallet obstructions at East exit are high severity. This is a standing rule."),
    (str(EU), "high", "medium",
     "Site policy: Matching 1.4 m proximity alerts at Dock 3 are medium severity. This is a standing rule."),
])
def test_severity_policy_needs_approval_and_changes_only_later_reviews(settings, admin, source_id, baseline, severity, reason):
    source_id = UUID(source_id)
    admin.execute("DELETE FROM review.events WHERE region_id = 'europe' AND id <> %s", (source_id,))
    worker = Worker(settings, "europe", "severity-test")
    store = EventStore(settings)
    worker.run_once()
    original = store.inspect_event("europe", source_id)["review"]
    assert original["result"]["decision"] == "confirmed" and original["result"]["severity"] == baseline
    worker.memory.correct(source_id, "supervisor-europe", CorrectionRequest(
        request_id=uuid4(), expected_version=0, decision="confirmed", severity=severity, reason=reason))
    worker.draft_once()
    suggestion = worker.memory.list_memory()["guidance"][0]
    assert suggestion["status"] == "proposed" and suggestion["draft_result"]["reusable"] is True
    assert suggestion["draft_result"]["text"] == reason

    pending = worker.memory.simulate(source_id, uuid4())
    worker.run_once()
    pending_detail = store.inspect_event("europe", pending["id"])
    unapproved = pending_detail["review"]
    assert pending_detail["attempts"][0]["context"]["memory"]["approved_guidance"] == []
    assert unapproved["result"]["decision"] == "needs_input"

    worker.memory.approve(suggestion["id"], "supervisor-europe", GuidanceApproval(
        text=suggestion["draft_result"]["text"]))
    later = worker.memory.simulate(source_id, uuid4())
    worker.run_once()
    later_detail = store.inspect_event("europe", later["id"])
    reviewed = later_detail["review"]
    assert reviewed["result"]["decision"] == "confirmed" and reviewed["result"]["severity"] == severity
    assert reason in reviewed["result"]["rationale"]
    selected = later_detail["attempts"][0]["context"]["memory"]["approved_guidance"]
    assert [item["id"] for item in selected] == [str(suggestion["id"])]
    assert store.inspect_event("europe", source_id)["review"] == original
    assert store.inspect_event("europe", pending["id"])["review"] == unapproved


@pytest.mark.parametrize("reason,conflict,overflow", [
    ("This particular event deserves higher severity because of a temporary condition.", False, False),
    ("Site policy: Matching exit obstructions are high severity.", True, False),
    ("Site policy: Matching exit obstructions are high severity.", False, True),
])
def test_mock_does_not_generalize_one_off_or_conflicting_severity_corrections(reason, conflict, overflow):
    reply = MockModel().draft({
        "event": {"event_type": "blocked_exit"},
        "original_review": {"decision": "confirmed", "severity": "medium"},
        "correction": {"decision": "confirmed", "severity": "high", "reason": reason},
        "memory": {"approved_guidance": [{"text": "Existing rule"}] if conflict else [], "guidance_overflow": overflow},
    })
    assert reply.output["reusable"] is False and reply.output["text"] is None


def test_competing_workers_cannot_claim_the_same_draft(settings, pending):
    ready = Barrier(2)
    def claim(owner):
        ready.wait(timeout=5)
        return DraftStore(settings, "europe").claim_next(owner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ["one", "two"]))
    assert sum(item is not None for item in claims) == 1


def test_expiry_reassignment_and_three_attempts(settings, admin, pending):
    worker, suggestion = pending
    store = worker.drafts
    old = start(store)
    expire(admin, suggestion["id"])
    new = start(store, "new-worker")
    assert old["draft_attempt_id"] != new["draft_attempt_id"]
    with pytest.raises(ClaimConflict):
        finish(store, old)
    expire(admin, suggestion["id"])
    third = start(store)
    expire(admin, suggestion["id"])
    assert store.claim_next("fourth") is None
    with pytest.raises(ClaimConflict):
        finish(store, third)
    saved = worker.memory.list_memory()["guidance"][0]
    assert saved["draft_status"] == "failed"
    assert [item["status"] for item in saved["draft_history"]] == ["expired"] * 3
    store.retry_failed(suggestion["id"])
    assert worker.draft_once()
    assert len(worker.memory.list_memory()["guidance"][0]["draft_history"]) == 4


@pytest.mark.parametrize("action", ["approve", "reject", "correct"])
def test_human_decision_invalidates_running_draft(pending, action):
    worker, suggestion = pending
    claim = start(worker.drafts)
    if action == "approve":
        worker.memory.approve(suggestion["id"], "supervisor-europe", GuidanceApproval(
            text="Human wording stays authoritative."))
    elif action == "reject":
        worker.memory.reject_or_retire(suggestion["id"], "supervisor-europe", "reject")
    else:
        worker.memory.correct(EU, "supervisor-europe", CorrectionRequest(request_id=uuid4(), expected_version=1,
                              decision="confirmed", severity="high", reason="The barrier was removed."))
    with pytest.raises(ClaimConflict):
        finish(worker.drafts, claim)
    row = next(item for item in worker.memory.list_memory()["guidance"] if item["id"] == suggestion["id"])
    assert row["draft_status"] == "cancelled" and row["draft_result"] is None
    assert row["proposed_text"] == REASON
    with pytest.raises(FeedbackConflict):
        worker.drafts.retry_failed(suggestion["id"])


def test_completed_draft_replay_is_idempotent_and_immutable(pending):
    worker, _ = pending
    claim = start(worker.drafts)
    saved = finish(worker.drafts, claim)
    assert finish(worker.drafts, claim) == saved
    with pytest.raises(ClaimConflict):
        finish(worker.drafts, claim, output=OUTPUT | {"text": "Overwritten"})
    with pytest.raises(ClaimConflict):
        worker.drafts.start(claim["id"], claim["draft_attempt_id"], claim["draft_worker_id"], {}, "changed")


def test_bad_output_retries_keep_usage_and_manual_retry_history(pending):
    worker, suggestion = pending
    class BadModel:
        name = "bad-test-model"
        def draft(self, *args, **kwargs):
            return ModelReply({"reusable": True, "text": "", "rationale": "Missing wording"}, {"provider": "codex", "output_tokens": 7})
    worker.model = BadModel()
    for _ in range(3):
        assert worker.draft_once()
    assert not worker.draft_once()
    row = worker.memory.list_memory()["guidance"][0]
    assert row["draft_status"] == "failed"
    assert all(item["usage"]["output_tokens"] == 7 for item in row["draft_history"])
    assert all("invalid guidance" in item["error"] for item in row["draft_history"])
    assert row["draft_result"] is None
    worker.drafts.retry_failed(suggestion["id"])
    assert len(worker.memory.list_memory()["guidance"][0]["draft_history"]) == 3


def test_uncertain_save_acknowledgement_does_not_repeat_model(pending, monkeypatch):
    worker, suggestion = pending
    finish_real = worker.drafts.finish
    def lost_ack(*args, **kwargs):
        finish_real(*args, **kwargs)
        raise psycopg.OperationalError("Test acknowledgement loss")
    monkeypatch.setattr(worker.drafts, "finish", lost_ack)
    with pytest.raises(psycopg.OperationalError):
        worker.draft_once()
    assert not worker.draft_once()
    row = worker.memory.list_memory()["guidance"][0]
    assert row["draft_status"] == "completed" and len(row["draft_history"]) == 1


def test_database_failure_leaves_claim_until_expiry(pending, monkeypatch):
    worker, _ = pending
    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("Test connection loss")
    monkeypatch.setattr(worker.drafts, "finish", unavailable)
    with pytest.raises(psycopg.OperationalError):
        worker.draft_once()
    assert not worker.draft_once()
    row = worker.memory.list_memory()["guidance"][0]
    assert row["draft_status"] == "running" and len(row["draft_history"]) == 1


def test_expired_or_wrong_worker_cannot_save_context(pending, admin):
    worker, suggestion = pending
    claim = worker.drafts.claim_next("owner")
    with pytest.raises(ClaimConflict):
        worker.drafts.start(claim["id"], claim["draft_attempt_id"], "other", {}, "wrong")
    expire(admin, suggestion["id"])
    with pytest.raises(ClaimConflict):
        worker.drafts.start(claim["id"], claim["draft_attempt_id"], "owner", {}, "late")
    row = worker.memory.list_memory()["guidance"][0]
    assert row["draft_history"][0]["context"] is None


def test_waiting_for_event_lock_does_not_preserve_an_expired_claim(pending, admin, monkeypatch):
    worker, suggestion = pending
    claim = start(worker.drafts)
    waiting = Event()
    lock = worker.drafts.memory._lock_guidance
    def observed_lock(*args):
        waiting.set()
        return lock(*args)
    monkeypatch.setattr(worker.drafts.memory, "_lock_guidance", observed_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with admin.transaction():
            admin.execute("SELECT id FROM review.events WHERE id = %s FOR UPDATE", (EU,))
            future = pool.submit(finish, worker.drafts, claim)
            assert waiting.wait(timeout=5)
            expire(admin, suggestion["id"])
        with pytest.raises(ClaimConflict):
            future.result(timeout=5)
    assert worker.memory.list_memory()["guidance"][0]["draft_result"] is None


def test_draft_context_limit_stops_call(pending, monkeypatch):
    worker, _ = pending
    monkeypatch.setattr(worker.memory, "select_for", lambda *args: {"too_big": "x" * 65537})
    class NotCalled:
        name = "not-called"
        def draft(self, *args, **kwargs):
            pytest.fail("Oversized context reached the model")
    worker.model = NotCalled()
    worker.draft_once()
    row = worker.memory.list_memory()["guidance"][0]
    assert row["draft_status"] == "queued" and row["draft_history"][0]["context"] is None


def test_late_reply_keeps_usage_but_never_becomes_guidance(pending, monkeypatch):
    worker, _ = pending
    clock = [0.0]
    monkeypatch.setattr("event_review.worker.monotonic", lambda: clock[0])
    class Late:
        name = "late-model"
        def draft(self, *args, **kwargs):
            clock[0] = 1000
            return ModelReply(OUTPUT, {"provider": "codex", "output_tokens": 10})
    worker.model = Late()
    worker.draft_once()
    row = worker.memory.list_memory()["guidance"][0]
    assert row["draft_result"] is None and row["draft_status"] == "queued"
    assert row["draft_history"][0]["error"] == "Model call timed out."
    assert row["draft_history"][0]["usage"]["output_tokens"] == 10


def test_region_and_api_retry_guards(settings, pending, client):
    worker, suggestion = pending
    assert DraftStore(settings, "us").claim_next("us") is None
    with pytest.raises(EventNotFound):
        DraftStore(settings, "us").retry_failed(suggestion["id"])
    with worker.claims.events.for_region("us") as connection:
        assert not connection.execute("SELECT * FROM review.guidance").fetchall()
    client.post("/api/session", json={"account_id": "supervisor-us"})
    assert client.post(f"/api/guidance/{suggestion['id']}/retry-draft").status_code == 404
    client.post("/api/session", json={"account_id": "supervisor-europe"})
    assert client.post(f"/api/guidance/{suggestion['id']}/retry-draft").status_code == 409
    for _ in range(3):
        claim = start(worker.drafts)
        finish(worker.drafts, claim, output=None, error="Test failure")
    assert client.post(f"/api/guidance/{suggestion['id']}/retry-draft").json()["draft_status"] == "queued"
    with worker.claims.events.for_region("europe") as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("UPDATE review.guidance SET proposed_text = 'changed' WHERE id = %s", (suggestion["id"],))


@pytest.mark.parametrize("changes", [{"reusable": "true"}, {"text": " "}, {"rationale": " "},
                                     {"reusable": False}, {"severity": "high"}, {"status": "approved"}])
def test_invalid_draft_cannot_change_scope_or_approve(changes):
    with pytest.raises(ValueError):
        GuidanceDraft.model_validate(OUTPUT | changes).check_result()
