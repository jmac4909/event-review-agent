from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

from event_review.claims import ClaimConflict, ClaimStore
from event_review.database import EventStore
from event_review.investigation import Investigation, InvestigationLimit
from event_review.models import Lookup, ModelReply
from event_review.reviewer import build_context
from event_review.worker import Worker


EU = UUID("00000001-0000-4000-8000-000000000001")
VERDICT = {"decision": "needs_input", "severity": None, "rationale": "Authorization is not established.",
           "question": "Was this particular person authorized?"}


@pytest.fixture
def event(settings, admin):
    admin.execute("DELETE FROM review.events WHERE region_id = 'europe' AND id <> %s", (EU,))
    return EventStore(settings).get_event("europe", EU)


def add_event(admin, source, *, region="europe", site=None, hours=-1, event_type="restricted_area_entry"):
    identifier = uuid4()
    admin.execute("""
        INSERT INTO review.events (id, region_id, source_ref, event_type, title, description,
                                   site, zone, camera, occurred_at, metadata)
        VALUES (%s, %s, %s, %s, 'Synthetic observation', 'Source evidence', %s, 'Aisle 2',
                'TEST-CAM', %s, '{}')
    """, (identifier, region, str(identifier), event_type, site or source["site"],
          source["occurred_at"] + timedelta(hours=hours)))
    return str(identifier)


def ask(name="find_events", hours=24, **extra):
    return {"decision": None, "severity": None, "question": None, "rationale": "Check relevant source evidence.",
            "lookup": {"name": name, "lookback_hours": hours, "event_type": None} | extra}


class ScriptedModel:
    name = "scripted-test-model"

    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.contexts = []

    def review(self, context, timeout_seconds=90):
        self.contexts.append(context)
        return ModelReply(next(self.outputs))


@pytest.mark.parametrize("last_output", [VERDICT, {"decision": "invalid"}])
def test_each_model_call_keeps_its_own_usage_even_when_the_verdict_is_invalid(settings, event, last_output):
    class ReportedModel(ScriptedModel):
        def review(self, context, timeout_seconds=90):
            reply = super().review(context, timeout_seconds)
            reply.usage = {"provider": "anthropic", "input_tokens": len(self.contexts) * 100,
                           "output_tokens": 20, "cache_read_input_tokens": 30}
            return reply

    model = ReportedModel([ask(), last_output])
    assert Worker(settings, "europe", "worker", model).run_once()
    saved = EventStore(settings).inspect_event("europe", EU)
    steps = saved["attempts"][0]["steps"]
    assert [step["kind"] for step in steps] == ["model", "tool", "model"]
    assert steps[0]["usage"]["input_tokens"] == 100
    assert steps[1]["usage"] is None
    assert steps[2]["usage"]["input_tokens"] == 200
    assert steps[2]["output"] == last_output
    assert all("usage" not in context for context in model.contexts)
    if last_output == VERDICT:
        assert saved["review"]["result"] == VERDICT
    else:
        assert saved["review"] is None and saved["status"] == "available"


def test_recent_events_are_scoped_bounded_and_never_look_into_future(settings, admin, event):
    expected = {add_event(admin, event) for _ in range(11)}
    add_event(admin, event, region="us")
    add_event(admin, event, site="Other site")
    add_event(admin, event, hours=1)
    add_event(admin, event, hours=-25)
    add_event(admin, event, event_type="blocked_exit")
    result = Investigation(settings, "europe").lookup(event, Lookup(name="find_events", lookback_hours=24, event_type="restricted_area_entry"))
    assert len(result["items"]) == 10 and result["more_available"] is True
    assert {item["id"] for item in result["items"]} <= expected
    assert all(item["region_id"] == "europe" and "status" not in item for item in result["items"])
    with pytest.raises(ValueError, match="region"):
        Investigation(settings, "us").lookup(event, Lookup(name="get_site_context"))


def test_site_rules_are_versioned_as_of_event_and_rls_is_read_only(settings, admin, event):
    admin.execute("""
        INSERT INTO review.site_context
        SELECT region_id, site, 2, valid_from + interval '1 month', 'future-rules', content
        FROM review.site_context WHERE region_id = 'europe'
    """)
    admin.execute("UPDATE review.site_context SET site = %s WHERE region_id = 'us'", (event["site"],))
    admin.execute("""
        INSERT INTO review.site_context
        SELECT region_id, 'Other site', 1, valid_from, 'other-site-rules', content
        FROM review.site_context WHERE region_id = 'europe' AND version = 1
    """)
    result = Investigation(settings, "europe").lookup(event, Lookup(name="get_site_context"))
    assert result["source"]["source_ref"] == "europe-synthetic-site-rules-v1"
    assert result["source"]["content"]["rules"]["pedestrian_vehicle_proximity"]["threshold_m"] == 2
    model = ScriptedModel([VERDICT])
    assert Worker(settings, "europe", "worker", model).run_once()
    rules = model.contexts[0]["site_rules"]
    assert rules == {"source_ref": result["source"]["source_ref"], "version": 1,
                     "valid_from": result["source"]["valid_from"], "rules": result["source"]["content"]["rules"]}
    with pytest.raises(ValueError, match="region"):
        Investigation(settings, "us").site_rules(event)
    with EventStore(settings).for_region("us") as connection:
        assert {row["region_id"] for row in connection.execute("SELECT region_id FROM review.site_context")} == {"us"}
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with EventStore(settings).for_region("europe") as connection:
            connection.execute("UPDATE review.site_context SET content = '{}' WHERE region_id = 'europe'")
    assert Investigation(settings, "europe").lookup(event | {"site": "Unconfigured"}, Lookup(name="get_site_context"))["source"] is None


@pytest.mark.parametrize("arguments", [
    {"name": "shell"}, {"name": "find_events", "lookback_hours": 0},
    {"name": "find_events", "lookback_hours": 169}, {"name": "find_events", "lookback_hours": True},
    {"name": "find_events"}, {"name": "get_site_context", "event_type": "anything"},
    {"name": "get_site_context", "region": "us"},
])
def test_lookup_arguments_cannot_expand_authority(arguments):
    with pytest.raises(ValueError):
        Lookup.model_validate(arguments).check_arguments()


def test_large_tool_result_is_rejected_instead_of_silently_truncated(settings, admin, event):
    identifier = add_event(admin, event)
    admin.execute("UPDATE review.events SET description = repeat('x', 17000) WHERE id = %s", (identifier,))
    with pytest.raises(InvestigationLimit, match="size limit"):
        Investigation(settings, "europe").lookup(event, Lookup(name="find_events", lookback_hours=24))


def test_owned_loop_records_each_input_before_call_and_supplies_tool_result(settings, admin, event):
    model = ScriptedModel([ask(), VERDICT])
    call = model.review

    def observe(context, timeout_seconds=90):
        stored = admin.execute("SELECT steps -> -1 -> 'input' FROM review.attempts WHERE event_id = %s", (EU,)).fetchone()[0]
        assert stored == context
        return call(context, timeout_seconds)

    model.review = observe
    assert Worker(settings, "europe", "worker", model).run_once()
    saved = EventStore(settings).inspect_event("europe", EU)
    attempt = saved["attempts"][0]
    assert saved["review"]["result"] == VERDICT
    assert [step["kind"] for step in attempt["steps"]] == ["model", "tool", "model"]
    assert attempt["context"]["memory_selection"]
    assert {key: value for key, value in attempt["context"].items() if key != "memory_selection"} == model.contexts[0]
    assert model.contexts[1]["tool_results"][0]["result"] == attempt["steps"][1]["output"]
    assert model.contexts[1]["lookups_remaining"] == 2
    assert all(step["finished_at"] >= step["started_at"] and step["error"] is None for step in attempt["steps"])


@pytest.mark.parametrize("requests, expected_calls, expected_steps", [
    ([ask(), ask()], 2, 3),
    ([ask(hours=1), ask(hours=2), ask(hours=3), ask(hours=4)], 4, 7),
])
def test_repeated_or_exhausted_lookups_wait_for_human_without_more_retries(settings, event, requests, expected_calls, expected_steps):
    model = ScriptedModel(requests)
    worker = Worker(settings, "europe", "worker", model)
    assert worker.run_once() and not worker.run_once()
    saved = EventStore(settings).inspect_event("europe", EU)
    assert saved["status"] == "needs_input" and saved["review"]["result"]["question"]
    assert len(saved["attempts"]) == 1
    attempt = saved["attempts"][0]
    assert len(model.contexts) == expected_calls and len(attempt["steps"]) == expected_steps
    assert attempt["model_output"]["source"] == "harness"


def test_invalid_lookup_is_retained_and_never_executed(settings, event, monkeypatch):
    model = ScriptedModel([ask(region="us")])
    worker = Worker(settings, "europe", "worker", model)
    monkeypatch.setattr(worker.investigation, "lookup", lambda *_: pytest.fail("Invalid tool was executed"))
    assert worker.run_once()
    attempt = EventStore(settings).inspect_event("europe", EU)["attempts"][0]
    assert attempt["status"] == "failed" and len(attempt["steps"]) == 1
    assert attempt["model_output"]["lookup"]["region"] == "us"


def test_context_budget_stops_before_a_model_call(settings, admin, event):
    admin.execute("UPDATE review.events SET description = repeat('x', 65000) WHERE id = %s", (EU,))
    model = ScriptedModel([])
    assert Worker(settings, "europe", "worker", model).run_once()
    saved = EventStore(settings).inspect_event("europe", EU)
    assert saved["status"] == "needs_input" and not model.contexts
    assert saved["attempts"][0]["steps"] == []


def test_upfront_rules_count_toward_context_budget(settings, admin, event):
    admin.execute("""
        UPDATE review.site_context SET content = jsonb_set(content, '{rules,large_rule}', to_jsonb(repeat('x', 65000)))
        WHERE region_id = 'europe'
    """)
    model = ScriptedModel([])
    assert Worker(settings, "europe", "worker", model).run_once()
    saved = EventStore(settings).inspect_event("europe", EU)
    assert saved["status"] == "needs_input" and not model.contexts
    assert saved["attempts"][0]["steps"] == []
    assert len(saved["attempts"][0]["context"]["site_rules"]["rules"]["large_rule"]) == 65000


def test_stale_tool_cannot_finish_trace_or_start_another_model_call(settings, admin, event, monkeypatch):
    model = ScriptedModel([ask(), VERDICT])
    worker = Worker(settings, "europe", "worker", model)
    lookup = worker.investigation.lookup
    replacement = []

    def expire(*args):
        result = lookup(*args)
        admin.execute("UPDATE review.attempts SET started_at = clock_timestamp() - interval '2 hours', expires_at = clock_timestamp() - interval '1 hour'")
        replacement.append(ClaimStore(settings, "europe").claim_next("replacement"))
        return result

    monkeypatch.setattr(worker.investigation, "lookup", expire)
    assert worker.run_once()
    saved = EventStore(settings).inspect_event("europe", EU)
    assert saved["review"] is None and saved["active_attempt_id"] == replacement[0]["id"]
    old = next(item for item in saved["attempts"] if item["worker_id"] == "worker")
    assert old["status"] == "expired" and old["steps"][-1]["finished_at"] is None
    assert len(model.contexts) == 1


def test_step_completion_cannot_rewrite_history_or_impersonate_worker(settings, event):
    claims = ClaimStore(settings, "europe")
    attempt = claims.claim_next("worker")
    identifier = attempt["id"]
    claims.start_review(EU, identifier, "worker", build_context(event), "test")
    number = claims.start_step(EU, identifier, "worker", "model", "test", {"event": str(EU)})
    with pytest.raises(ClaimConflict):
        claims.start_step(EU, identifier, "worker", "model", "test", {})
    with pytest.raises(ClaimConflict):
        claims.finish_step(EU, identifier, "other-worker", number, VERDICT)
    usage = {"provider": "codex", "input_tokens": 100, "output_tokens": 20}
    claims.finish_step(EU, identifier, "worker", number, VERDICT, usage=usage)
    with pytest.raises(ClaimConflict):
        claims.finish_step(EU, identifier, "worker", number, {"changed": True}, usage={"input_tokens": 999})
    step = EventStore(settings).inspect_event("europe", EU)["attempts"][0]["steps"][0]
    assert step["output"] == VERDICT and step["usage"] == usage


def test_uncertain_trace_save_does_not_release_claim_or_repeat_model(settings, event, monkeypatch):
    model = ScriptedModel([VERDICT])
    worker = Worker(settings, "europe", "worker", model)
    save = worker.claims.save_output

    def lose_ack(*args):
        save(*args)
        raise psycopg.OperationalError("Synthetic lost trace acknowledgement")

    monkeypatch.setattr(worker.claims, "save_output", lose_ack)
    with pytest.raises(psycopg.OperationalError):
        worker.run_once()
    assert not worker.run_once() and len(model.contexts) == 1
    saved = EventStore(settings).inspect_event("europe", EU)
    assert saved["status"] == "claimed" and saved["review"] is None
    assert saved["attempts"][0]["model_output"] == VERDICT
