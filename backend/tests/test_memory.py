from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
import pytest

from event_review.claims import ClaimStore
from event_review.database import EventNotFound, EventStore
from event_review.investigation import Investigation
from event_review.memory import FeedbackConflict, MemoryStore
from event_review.models import CorrectionRequest, GuidanceApproval, ModelReply
from event_review.reviewer import MockModel, build_context
from event_review.retrieval import MAX_GUIDANCE
from event_review.worker import Worker


EU = UUID("00000001-0000-4000-8000-000000000001")
US = UUID("00000002-0000-4000-8000-000000000001")
SUPERVISOR = "supervisor-europe"
REASON = "A fixed barrier separates this walkway from the vehicle route."


class RecordingEncoder:
    model_name = "test-encoder"
    revision = "test-revision"

    def __init__(self):
        self.calls = []

    def encode(self, texts):
        self.calls.append(texts)
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture
def memory(settings, admin):
    admin.execute("DELETE FROM review.events WHERE region_id = 'europe' AND id <> %s", (EU,))
    return MemoryStore(settings, "europe")


def correct(memory, event_id=EU, version=0, decision="rejected", reason=REASON, request_id=None):
    body = CorrectionRequest(request_id=request_id or uuid4(), expected_version=version, decision=decision,
                             severity="low" if decision == "confirmed" else None, reason=reason)
    return memory.correct(event_id, SUPERVISOR, body)


def approve(memory, correction, text=REASON):
    proposal = next(item for item in memory.list_memory()["guidance"] if item["source_correction_id"] == correction["id"])
    return memory.approve(proposal["id"], SUPERVISOR,
                          GuidanceApproval(text=text))


def review(settings):
    assert Worker(settings, "europe", "test-worker").run_once()


def test_answer_resolves_needs_input_and_preserves_original_and_history(settings, admin, memory):
    admin.execute("UPDATE review.events SET event_type = 'restricted_area_entry' WHERE id = %s", (EU,))
    review(settings)
    original = EventStore(settings).inspect_event("europe", EU)["review"]
    first = correct(memory, reason="This specific person was authorized for the scheduled maintenance.")
    saved = EventStore(settings).inspect_event("europe", EU)
    assert saved["status"] == "reviewed"
    assert saved["review"] == original and original["result"]["decision"] == "needs_input"
    assert saved["corrections"][0]["id"] == str(first["id"])
    second = correct(memory, version=1, decision="confirmed", reason="Correction: the permit had expired.")
    saved = EventStore(settings).inspect_event("europe", EU)
    assert [row["version"] for row in saved["corrections"]] == [2, 1]
    assert second["supervisor_id"] == SUPERVISOR
    assert {item["status"] for item in memory.list_memory()["guidance"]} == {"proposed", "superseded"}
    later = memory.simulate(EU, uuid4())
    review(settings)
    later = EventStore(settings).inspect_event("europe", later["id"])
    assert later["review"]["result"]["decision"] == "needs_input"
    assert later["attempts"][0]["context"]["memory"]["approved_guidance"] == []


def test_correction_replay_is_idempotent_even_after_a_newer_version(settings, memory):
    review(settings)
    request_id = uuid4()
    first = correct(memory, request_id=request_id)
    assert correct(memory, request_id=request_id) == first
    correct(memory, version=1, decision="confirmed")
    assert correct(memory, request_id=request_id) == first
    assert len(memory.list_memory()["corrections"]) == 2
    with pytest.raises(FeedbackConflict, match="different feedback"):
        correct(memory, reason="Changed replay", request_id=request_id)
    with pytest.raises(FeedbackConflict, match="Another supervisor"):
        correct(memory, version=0)


def test_concurrent_supervisor_edits_do_not_overwrite_each_other(settings, memory):
    review(settings)
    ready = Barrier(2)

    def submit(reason):
        ready.wait(timeout=5)
        try:
            return correct(memory, reason=reason)["version"]
        except FeedbackConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["First supervisor evidence", "Second supervisor evidence"]))
    assert sorted(map(str, results)) == ["1", "conflict"]
    assert len(memory.list_memory()["corrections"]) == len(memory.list_memory()["guidance"]) == 1


def test_feedback_waits_for_a_review_and_rejects_foreign_ids(memory):
    with pytest.raises(FeedbackConflict, match="Wait for"):
        correct(memory)
    with pytest.raises(EventNotFound):
        correct(memory, event_id=US)
    assert memory.list_memory() == {"corrections": [], "guidance": []}


def test_legacy_approval_conflict_stays_visible_and_is_not_silently_reapproved(settings, memory, admin):
    review(settings)
    correction = correct(memory)
    proposal = memory.list_memory()["guidance"][0]
    assert proposal["source_decision"] == "rejected" and proposal["source_severity"] is None
    approve(memory, correction)
    # Represent a legacy record created before approval inherited its outcome.
    admin.execute("UPDATE review.guidance SET decision='confirmed', severity='high' WHERE id=%s", (proposal["id"],))
    saved = memory.list_memory()["guidance"][0]
    assert saved["decision"] == "confirmed" and saved["severity"] == "high"
    assert saved["source_decision"] == correction["decision"] == "rejected"
    assert saved["source_severity"] is None
    with pytest.raises(FeedbackConflict, match="already approved"):
        approve(memory, correction)
    assert memory.list_memory()["guidance"][0] == saved


@pytest.mark.parametrize("decision,severity", [("rejected", None), ("confirmed", "low"),
                                              ("confirmed", "medium"), ("confirmed", "high")])
def test_approval_api_inherits_the_immutable_correction_not_proposal_fields(settings, memory, client, admin, decision, severity):
    review(settings)
    correction = memory.correct(EU, SUPERVISOR, CorrectionRequest(
        request_id=uuid4(), expected_version=0, decision=decision, severity=severity, reason=REASON))
    proposal = memory.list_memory()["guidance"][0]
    # The correction must remain authoritative even if a pending copy differs.
    other = ("confirmed", "high") if decision == "rejected" else ("rejected", None)
    admin.execute("UPDATE review.guidance SET decision=%s, severity=%s WHERE id=%s", (*other, proposal["id"]))
    client.post("/api/session", json={"account_id": SUPERVISOR})
    url = f"/api/guidance/{proposal['id']}/approve"
    body = {"text": "Use the corrected outcome for matching walkway events."}
    response = client.post(url, json=body)
    assert response.status_code == 200
    saved = response.json()
    assert (saved["decision"], saved["severity"]) == (decision, severity)
    assert saved["status"] == "approved" and saved["source_correction_id"] == str(correction["id"])
    assert client.post(url, json=body).json() == saved
    assert memory.list_memory()["corrections"][0] == correction


@pytest.mark.parametrize("change", [{"decision": "confirmed"}, {"severity": "high"},
    {"decision": "rejected", "severity": None}, {"source_correction_id": str(US)},
    {"text": " "}, {"text": ""}, {"text": "yes"}, {"text": "ok"}, {"text": "x" * 2001}])
def test_approval_api_rejects_overrides_and_invalid_wording_without_writes(settings, memory, client, change):
    review(settings)
    correct(memory)
    before = memory.list_memory()
    client.post("/api/session", json={"account_id": SUPERVISOR})
    response = client.post(f"/api/guidance/{before['guidance'][0]['id']}/approve", json={"text": REASON} | change)
    assert response.status_code == 422
    assert memory.list_memory() == before


def test_before_correction_approval_and_later_review_have_inspectable_memory(settings, memory):
    review(settings)
    original = EventStore(settings).inspect_event("europe", EU)
    assert original["review"]["result"]["decision"] == "confirmed"
    correction = correct(memory)
    before_approval = memory.simulate(EU, uuid4())
    review(settings)
    pending = EventStore(settings).inspect_event("europe", before_approval["id"])
    assert pending["review"]["result"]["decision"] == "needs_input"
    supplied = pending["attempts"][0]["context"]["memory"]
    assert supplied["corrections"][0]["id"] == str(correction["id"])
    assert supplied["approved_guidance"] == []
    guidance = approve(memory, correction, text="Verified fixed barrier; the detector projects across it.")
    later = MemoryStore(settings, "europe").simulate(EU, uuid4())
    review(settings)
    after = EventStore(settings).inspect_event("europe", later["id"])
    assert after["review"]["result"]["decision"] == "needs_input"
    assert "Mock cannot interpret" in after["review"]["result"]["rationale"]
    selected = after["attempts"][0]["context"]["memory"]["approved_guidance"][0]
    assert selected["id"] == str(guidance["id"])
    assert selected["text"] == guidance["approved_text"] and "proposed_text" not in selected
    assert EventStore(settings).inspect_event("europe", EU)["review"] == original["review"]
    assert memory.list_memory()["guidance"][0]["proposed_text"] == REASON


@pytest.mark.parametrize("field", ["site", "zone", "camera", "event_type"])
def test_guidance_requires_location_and_event_type_match(settings, memory, field):
    review(settings)
    approve(memory, correct(memory))
    event = EventStore(settings).get_event("europe", EU)
    event["id"] = uuid4()
    event[field] = "a different value"
    encoder = RecordingEncoder()
    selected = memory.select_for(event, encoder)
    assert selected["approved_guidance"] == []
    assert selected["corrections"] == []
    assert encoder.calls == []
    event["region_id"] = "us"
    with pytest.raises(ValueError, match="region"):
        memory.select_for(event, encoder)


def test_approved_exception_does_not_replace_missing_baseline_rules(settings, admin, memory):
    review(settings)
    approve(memory, correct(memory))
    later = memory.simulate(EU, uuid4())
    admin.execute("DELETE FROM review.site_context WHERE region_id = 'europe'")
    review(settings)
    saved = EventStore(settings).inspect_event("europe", later["id"])
    context = saved["attempts"][0]["context"]
    assert context["site_rules"] is None and len(context["memory"]["approved_guidance"]) == 1
    assert saved["status"] == "needs_input"
    assert saved["review"]["result"]["question"] == "Which site rules apply?"


@pytest.mark.parametrize("status", ["proposed", "rejected", "retired", "superseded"])
def test_unapproved_guidance_text_never_reaches_the_encoder(settings, memory, admin, status):
    review(settings)
    correction = correct(memory)
    guidance = approve(memory, correction, text="APPROVED-TEXT-ONLY applies to matching walkway events.")
    admin.execute("UPDATE review.guidance SET status = %s WHERE id = %s", (status, guidance["id"]))
    encoder = RecordingEncoder()
    event = EventStore(settings).get_event("europe", EU) | {"id": uuid4()}
    selected = memory.select_for(event, encoder)
    assert selected["approved_guidance"] == []
    if status == "retired":
        assert selected["corrections"] == encoder.calls == []
    else:
        assert selected["corrections"][0]["reason"] == REASON
        assert encoder.calls and "APPROVED-TEXT-ONLY" not in str(encoder.calls)


def test_foreign_region_memory_cannot_reach_the_encoder(settings, memory):
    review(settings)
    approve(memory, correct(memory))
    # Give the foreign event identical matching fields to make region the only
    # exclusion. A dropped region predicate must still be caught by RLS.
    event = EventStore(settings).get_event("europe", EU) | {"id": uuid4(), "region_id": "us"}
    encoder = RecordingEncoder()
    selected = MemoryStore(settings, "us").select_for(event, encoder)
    assert selected["corrections"] == selected["approved_guidance"] == encoder.calls == []


def test_candidate_window_is_bounded_before_encoding_and_records_omissions(settings, memory):
    review(settings)
    claims = ClaimStore(settings, "europe")
    corrections = []
    for index in range(66):
        event = memory.simulate(EU, uuid4())
        attempt = claims.claim_next("fixture-worker")
        assert attempt["event_id"] == event["id"]
        claims.complete(event["id"], attempt["id"], "fixture-worker",
                        {"decision": "rejected", "severity": None, "rationale": "Fixture", "question": None})
        corrections.append(correct(memory, event_id=event["id"], reason=f"Case {index}: {REASON}"))
    encoder = RecordingEncoder()
    selected = memory.select_for(EventStore(settings).get_event("europe", EU), encoder)
    assert [len(batch) for batch in encoder.calls] == [65]
    assert len(selected["corrections"]) == 3
    selection = selected["selection"]
    assert selection["method"] == "hybrid" and selection["model_revision"] == "test-revision"
    assert selection["more_cases_available"] is True
    assert len(selection["case_scores"]) == 64
    ids = {item["id"] for item in selection["case_scores"]}
    assert str(corrections[0]["id"]) not in ids and str(corrections[-1]["id"]) in ids
    assert sum(item["selected"] for item in selection["case_scores"]) == 3


def test_harness_requires_supervision_instead_of_silently_omitting_excess_guidance(settings, memory):
    event_id = EU
    count = MAX_GUIDANCE + 1
    for index in range(count):
        review(settings)
        approve(memory, correct(memory, event_id=event_id))
        event_id = memory.simulate(EU, uuid4())["id"]

    calls = []

    class BlindModel:
        name = "test-ignores-guidance"

        def review(self, context, timeout_seconds=90):
            calls.append(context)
            return ModelReply({"decision": "rejected", "severity": None, "rationale": "Ignored guidance", "question": None})

    assert Worker(settings, "europe", "guarded-worker", BlindModel()).run_once()
    saved = EventStore(settings).inspect_event("europe", event_id)
    assert saved["status"] == "needs_input" and calls == []
    attempt = saved["attempts"][0]
    assert attempt["steps"] == [] and attempt["model_output"]["source"] == "harness"
    assert attempt["context"]["memory"]["guidance_overflow"] is True
    assert len(attempt["context"]["memory"]["approved_guidance"]) == MAX_GUIDANCE
    assert len(attempt["context"]["memory"]["corrections"]) <= 3


@pytest.mark.parametrize("count", [4, MAX_GUIDANCE])
def test_model_receives_all_scoped_rules_even_with_different_source_outcomes(settings, memory, count):
    event_id = EU
    approved = []
    for index in range(count):
        review(settings)
        wording = f"For a verified distance between {index} and {index + 1} metres, use this corrected outcome."
        correction = correct(memory, event_id=event_id, reason=wording,
                             decision="confirmed" if index % 2 else "rejected")
        approved.append(approve(memory, correction, text=wording))
        event_id = memory.simulate(EU, uuid4())["id"]

    calls = []

    class InspectingModel:
        name = "test-guidance-context"

        def review(self, context, timeout_seconds=90):
            calls.append(context)
            received = context["memory"]["approved_guidance"]
            assert {row["id"] for row in received} == {str(row["id"]) for row in approved}
            assert {row["decision"] for row in received} == {"confirmed", "rejected"}
            assert all("metadata_condition" not in row for row in received)
            # This test proves the provider gets the full set. Its response is
            # scripted; live model interpretation is a separate evaluation.
            return ModelReply({"decision": "needs_input", "severity": None,
                               "rationale": "Scripted response after inspecting every supplied rule.",
                               "question": "Which rule covers the boundary?"})

    Worker(settings, "europe", "condition-review", InspectingModel()).run_once()
    saved = EventStore(settings).inspect_event("europe", event_id)
    assert len(calls) == 1
    assert saved["review"]["result"]["question"] == "Which rule covers the boundary?"
    assert saved["attempts"][0]["steps"][0]["input"] == calls[0]


@pytest.mark.parametrize("wording", [
    "Dismiss alerts only when the current distance is greater than 2 metres.",
    "Dismiss entries on November 12, 2026 only. Ask for authorization on other dates.",
    "Dismiss alerts only when a supervisor has verified the separating barrier is intact.",
])
def test_mock_never_copies_arbitrary_conditional_guidance(settings, memory, wording):
    review(settings)
    approve(memory, correct(memory, reason=wording), text=wording)
    event = memory.simulate(EU, uuid4())
    review(settings)
    saved = EventStore(settings).inspect_event("europe", event["id"])
    assert saved["review"]["result"]["decision"] == "needs_input"
    assert "Mock cannot interpret" in saved["review"]["result"]["rationale"]
    assert saved["attempts"][0]["context"]["memory"]["approved_guidance"][0]["text"] == wording


def test_rejected_retired_and_superseded_guidance_never_become_trusted(settings, memory):
    review(settings)
    first = correct(memory)
    proposal = memory.list_memory()["guidance"][0]
    memory.reject_or_retire(proposal["id"], SUPERVISOR, "reject")
    with pytest.raises(FeedbackConflict):
        approve(memory, first)
    second = correct(memory, version=1)
    approved = approve(memory, second)
    memory.reject_or_retire(approved["id"], SUPERVISOR, "retire")
    with pytest.raises(FeedbackConflict):
        approve(memory, second)
    third = correct(memory, version=2)
    old = approve(memory, third)
    correct(memory, version=3, decision="confirmed")
    by_id = {row["id"]: row for row in memory.list_memory()["guidance"]}
    assert by_id[old["id"]]["status"] == "superseded"
    assert memory.select_for(EventStore(settings).get_event("europe", EU))["approved_guidance"] == []


def test_approval_replay_cannot_change_an_existing_human_decision(settings, memory):
    review(settings)
    correction = correct(memory)
    first = approve(memory, correction)
    assert approve(memory, correction) == first
    with pytest.raises(FeedbackConflict):
        approve(memory, correction, text="Changed rule for future matching events.")
    assert memory.list_memory()["guidance"][0]["approved_text"] == REASON


def test_source_detail_tracks_current_guidance_status_and_latest_correction(settings, memory, client):
    review(settings)
    client.post("/api/session", json={"account_id": SUPERVISOR})
    url = f"/api/events/{EU}"
    assert client.get(url).json()["guidance"] is None
    first = correct(memory)
    proposal = memory.list_memory()["guidance"][0]
    detail = client.get(url).json()
    assert detail["guidance"] == {"id": str(proposal["id"]), "status": "proposed", "source_correction_id": str(first["id"])}
    approved = approve(memory, first)
    assert client.get(url).json()["guidance"]["status"] == "approved"
    memory.reject_or_retire(approved["id"], SUPERVISOR, "retire")
    assert client.get(url).json()["guidance"]["status"] == "retired"
    second = correct(memory, version=1)
    detail = client.get(url).json()
    assert detail["guidance"]["status"] == "proposed"
    assert detail["guidance"]["source_correction_id"] == detail["corrections"][0]["id"] == str(second["id"])
    memory.reject_or_retire(UUID(detail["guidance"]["id"]), SUPERVISOR, "reject")
    assert client.get(url).json()["guidance"]["status"] == "rejected"


def test_retirement_does_not_rewrite_an_in_flight_context_snapshot(settings, memory):
    review(settings)
    guidance = approve(memory, correct(memory))
    later = memory.simulate(EU, uuid4())

    class RetiringModel(MockModel):
        def review(self, context, timeout_seconds=90):
            memory.reject_or_retire(guidance["id"], SUPERVISOR, "retire")
            assert context["memory"]["approved_guidance"][0]["id"] == str(guidance["id"])
            return ModelReply({"decision": "rejected", "severity": None,
                               "rationale": "Test response using the saved snapshot.", "question": None})

    assert Worker(settings, "europe", "in-flight", RetiringModel()).run_once()
    saved = EventStore(settings).inspect_event("europe", later["id"])
    assert saved["review"]["result"]["decision"] == "rejected"
    assert saved["attempts"][0]["context"]["memory"]["approved_guidance"][0]["id"] == str(guidance["id"])
    assert memory.select_for(later)["approved_guidance"] == []


def test_simulation_is_idempotent_and_does_not_reset_source(settings, memory, admin):
    review(settings)
    request_id = uuid4()
    ready = Barrier(2)

    def simulate(_):
        ready.wait(timeout=5)
        return memory.simulate(EU, request_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(simulate, range(2)))
    assert results[0] == results[1] and results[0]["metadata"]
    assert results[0]["simulated_from_event_id"] == EU
    assert EventStore(settings).get_event("europe", EU)["status"] == "reviewed"
    assert admin.execute("SELECT count(*) FROM review.events WHERE region_id = 'europe'").fetchone()[0] == 2
    with pytest.raises(FeedbackConflict):
        memory.simulate(request_id, request_id)
    with pytest.raises(EventNotFound):
        memory.simulate(US, uuid4())


@pytest.mark.parametrize("change", [{"reason": " "}, {"decision": "confirmed"}, {"severity": "high"},
                                    {"region_id": "us"}, {"supervisor_id": "someone-else"}, {"expected_version": "0"}])
def test_feedback_api_validates_input_without_writes(settings, client, memory, change):
    review(settings)
    client.post("/api/session", json={"account_id": SUPERVISOR})
    body = {"request_id": str(uuid4()), "expected_version": 0, "decision": "rejected", "severity": None, "reason": REASON}
    response = client.post(f"/api/events/{EU}/corrections", json=body | change)
    assert response.status_code == 422
    assert memory.list_memory() == {"corrections": [], "guidance": []}


def test_api_and_rls_keep_feedback_regional(settings, client, memory):
    review(settings)
    correction = correct(memory)
    guidance = approve(memory, correction)
    assert client.get("/api/memory").status_code == 401
    assert client.post(f"/api/guidance/{guidance['id']}/approve", json={"text": REASON}).status_code == 401
    assert client.post(f"/api/guidance/{guidance['id']}/retire").status_code == 401
    client.post("/api/session", json={"account_id": "supervisor-us"})
    assert client.get("/api/memory").json() == {"corrections": [], "guidance": []}
    assert client.post(f"/api/guidance/{guidance['id']}/retire").status_code == 404
    assert client.post(f"/api/guidance/{guidance['id']}/approve", json={"text": REASON}).status_code == 404
    assert client.post(f"/api/events/{EU}/simulate", json={"request_id": str(uuid4())}).status_code == 404
    with EventStore(settings).for_region("us") as connection:
        assert connection.execute("SELECT * FROM review.corrections").fetchall() == []
        assert connection.execute("SELECT * FROM review.guidance").fetchall() == []
    client.post("/api/session", json={"account_id": SUPERVISOR})
    assert client.post(f"/api/guidance/{guidance['id']}/retire", headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.post(f"/api/events/{EU}/simulate", json={"request_id": str(uuid4()), "region": "us"}).status_code == 422
    assert client.get(f"/api/events/{EU}").json()["corrections"][0]["reason"] == REASON


def test_foreign_parent_links_and_history_edits_are_rejected(settings, memory, admin):
    review(settings)
    correction = correct(memory)
    with EventStore(settings).for_region("us") as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute("""
                INSERT INTO review.corrections (id, event_id, region_id, version, decision, reason, supervisor_id, event_snapshot)
                VALUES (%s, %s, 'us', 99, 'rejected', 'test', 'supervisor-us', '{}')
            """, (uuid4(), EU))
    # Remove only this disposable fixture's proposal to exercise the foreign key
    # independently of the one-proposal-per-correction uniqueness constraint.
    admin.execute("DELETE FROM review.guidance WHERE source_correction_id = %s", (correction["id"],))
    with EventStore(settings).for_region("us") as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute("""
                INSERT INTO review.guidance (id, region_id, source_event_id, source_correction_id,
                    event_type, site, zone, camera, metadata_condition, proposed_text, decision)
                VALUES (%s, 'us', %s, %s, 'test', 'test', 'test', 'test', '{}', 'test', 'rejected')
            """, (uuid4(), EU, correction["id"]))
    for query in ("UPDATE review.corrections SET reason = 'rewrite'", "DELETE FROM review.corrections",
                  "UPDATE review.guidance SET camera = 'new scope'", "UPDATE review.guidance SET proposed_text = 'rewrite'"):
        with EventStore(settings).for_region("europe") as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(query)


def test_new_correction_cannot_race_with_approval_of_old_guidance(settings, memory):
    review(settings)
    original = correct(memory)
    ready = Barrier(2)

    def approve_old():
        ready.wait(timeout=5)
        try:
            return approve(memory, original)["status"]
        except FeedbackConflict:
            return "conflict"

    def replace_source():
        ready.wait(timeout=5)
        return correct(memory, version=1, decision="confirmed")["version"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        approval = pool.submit(approve_old)
        replacement = pool.submit(replace_source)
        assert approval.result(timeout=10) in ("approved", "conflict")
        assert replacement.result(timeout=10) == 2
    items = memory.list_memory()["guidance"]
    assert next(item for item in items if item["source_correction_id"] == original["id"])["status"] == "superseded"
    assert memory.select_for(EventStore(settings).get_event("europe", EU))["approved_guidance"] == []


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("score", [0.88, None])
def test_confidence_change_or_absence_reuses_new_and_legacy_guidance(settings, memory, admin, legacy, score):
    admin.execute("UPDATE review.events SET metadata=jsonb_set(metadata, '{detector_score}', '0.89') WHERE id=%s", (EU,))
    review(settings)
    guidance = approve(memory, correct(memory))
    assert "detector_score" not in guidance["metadata_condition"]
    if legacy:
        # Existing approved rows keep their historical stored conditions.
        admin.execute("UPDATE review.guidance SET metadata_condition=jsonb_set(metadata_condition, '{detector_score}', '0.89') WHERE id=%s", (guidance["id"],))
    event = EventStore(settings).get_event("europe", EU) | {"id": uuid4()}
    assert memory.select_for(event)["approved_guidance"][0]["id"] == str(guidance["id"])
    event["metadata"] = dict(event["metadata"])
    if score is None:
        event["metadata"].pop("detector_score")
    else:
        event["metadata"]["detector_score"] = score
    selected = memory.select_for(event)
    assert selected["approved_guidance"][0]["id"] == str(guidance["id"])
    assert "metadata_condition" not in selected["approved_guidance"][0]
    assert selected["approved_guidance"][0]["event_snapshot"]["metadata"]["detector_score"] == 0.89
    assert EventStore(settings).get_event("europe", EU)["metadata"]["detector_score"] == 0.89
    context = build_context(event, selected, Investigation(settings, "europe").site_rules(event))
    assert MockModel().review(context).output["decision"] == "needs_input"


@pytest.mark.parametrize("event_id,field,value", [
    (EU, "estimated_distance_m", 1.7),
    (EU, "estimated_distance_m", 3.5),
    (EU, "people_count", 7),
    (EU, "vehicle_count", 0),
    (EU, "detector", "proximity-v3"),
    (EU, "new_evidence", {"barrier_verified": False}),
    (UUID("00000001-0000-4000-8000-000000000003"), "observed_duration_seconds", 121),
])
def test_changed_measurements_remain_available_for_model_interpretation(settings, admin, event_id, field, value):
    memory = MemoryStore(settings, "europe")
    admin.execute("UPDATE review.events SET metadata=jsonb_set(metadata, '{detector_score}', '0.89') WHERE id=%s", (event_id,))
    worker = Worker(settings, "europe", "measurement-test")
    while worker.run_once():
        pass
    guidance = approve(memory, correct(memory, event_id=event_id))
    event = EventStore(settings).get_event("europe", event_id) | {"id": uuid4()}
    event["metadata"] = event["metadata"] | {"detector_score": 0.88}
    assert len(memory.select_for(event)["approved_guidance"]) == 1
    event["metadata"][field] = value
    selected = memory.select_for(event, RecordingEncoder())
    assert selected["approved_guidance"][0]["id"] == str(guidance["id"])
    assert selected["approved_guidance"][0]["event_snapshot"]["metadata"].get(field) != value
    context = build_context(event, selected, Investigation(settings, "europe").site_rules(event))
    assert context["event"]["metadata"][field] == value
    # Unsupported policy needs interpretation; Mock must not copy its outcome.
    assert MockModel().review(context).output["decision"] == "needs_input"
    event["metadata"] = {}
    assert memory.select_for(event)["approved_guidance"][0]["id"] == str(guidance["id"])


def test_dated_guidance_keeps_its_conditions_and_event_date_in_context(settings, memory):
    from datetime import datetime, timezone

    review(settings)
    wording = "On November 12, 2026 only, dismiss matching alerts under this temporary site exception."
    guidance = approve(memory, correct(memory, reason=wording), text=wording)
    event = EventStore(settings).get_event("europe", EU) | {"id": uuid4()}
    for day in (12, 14):
        event["occurred_at"] = datetime(2026, 11, day, 12, tzinfo=timezone.utc)
        selected = memory.select_for(event)
        assert selected["approved_guidance"][0]["id"] == str(guidance["id"])
        context = build_context(event, selected)
        assert context["memory"]["approved_guidance"][0]["text"] == wording
        assert context["event"]["occurred_at"].startswith(f"2026-11-{day}")
    # Dates are currently model-interpreted text, not a database expiry filter.


@pytest.mark.parametrize("has_older_correction", [False, True])
def test_stopped_guidance_removes_its_correction_from_future_reviews(settings, memory, has_older_correction):
    review(settings)
    original = EventStore(settings).inspect_event("europe", EU)["review"]
    if has_older_correction:
        correct(memory, reason="An older case correction must not return after the latest rule is stopped.")
    correction = correct(memory, version=int(has_older_correction))
    guidance = approve(memory, correction)
    before = memory.simulate(EU, uuid4())
    review(settings)
    before_detail = EventStore(settings).inspect_event("europe", before["id"])
    assert before_detail["review"]["result"]["decision"] == "needs_input"
    assert before_detail["attempts"][0]["context"]["memory"]["corrections"][0]["id"] == str(correction["id"])

    memory.reject_or_retire(guidance["id"], SUPERVISOR, "retire")
    later = memory.simulate(EU, uuid4())
    encoder = RecordingEncoder()
    selected = memory.select_for(later, encoder)
    assert selected["corrections"] == selected["approved_guidance"] == encoder.calls == []
    review(settings)
    saved = EventStore(settings).inspect_event("europe", later["id"])
    assert saved["review"]["result"] == original["result"]
    assert saved["review"]["result"]["question"] is None
    supplied = saved["attempts"][0]["context"]["memory"]
    assert supplied["corrections"] == supplied["approved_guidance"] == []

    history = memory.list_memory()
    assert correction in history["corrections"]
    assert next(item for item in history["guidance"] if item["id"] == guidance["id"])["status"] == "retired"
    assert EventStore(settings).inspect_event("europe", EU)["review"] == original
    assert EventStore(settings).inspect_event("europe", before["id"])["attempts"] == before_detail["attempts"]


def test_stopping_one_rule_keeps_other_and_new_corrections_available(settings, memory):
    review(settings)
    stopped = correct(memory)
    guidance = approve(memory, stopped)
    memory.reject_or_retire(guidance["id"], SUPERVISOR, "retire")
    other_event = memory.simulate(EU, uuid4())
    review(settings)
    other = correct(memory, event_id=other_event["id"])
    target = memory.simulate(EU, uuid4())
    assert {item["id"] for item in memory.select_for(target)["corrections"]} == {str(other["id"])}
    newer = correct(memory, version=1, reason="New supervisor evidence recorded after the earlier rule was stopped.")
    selected = memory.select_for(target)
    assert {item["id"] for item in selected["corrections"]} == {str(other["id"]), str(newer["id"])}
    assert selected["approved_guidance"] == []
