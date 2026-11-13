"""Evaluate rule interpretation with a real provider in one disposable database.

Uses scripted supervisor approvals, never live feedback. Each review uses the
normal worker, retrieval, claims and provider path. No automatic eval retries.
"""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/src"))
from event_review.bootstrap import bootstrap
from event_review.database import EventStore
from event_review.memory import MemoryStore
from event_review.models import CorrectionRequest, GuidanceApproval
from event_review.settings import Settings
from event_review.worker import Worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("codex", "anthropic"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise SystemExit("Keep the previous report; choose a new output path.")
    base = Settings.from_env()
    database = f"review_guidance_{uuid4().hex[:12]}"
    settings = replace(base, pg_database=database, model_mode=args.mode)
    real = Worker(settings, "europe", "guidance-evaluation")
    mock = Worker(replace(settings, model_mode="mock"), "europe", "fixture-worker")
    store, memory = EventStore(settings), MemoryStore(settings, "europe")
    report = {"provider": real.model.name, "scripted_supervisor_fixtures": True,
              "initial_reviews": [], "drafts": [], "cases": []}
    bootstrap(settings.pg_host, settings.pg_port, database)
    try:
        with psycopg.connect(host=settings.pg_host, port=settings.pg_port,
                             dbname=database, user="review_bootstrap", autocommit=True) as admin:
            while mock.run_once():
                pass

            def event(number, metadata, date="2026-11-14T12:00:00Z", scenario=None):
                source = UUID(f"00000001-0000-4000-8000-{number:012d}")
                if scenario is not None:
                    return memory.simulate(source, uuid4(), scenario)["id"]
                created = memory.simulate(source, uuid4())
                facts = created["metadata"] | metadata
                admin.execute("UPDATE review.events SET metadata=%s, occurred_at=%s WHERE id=%s",
                              (Jsonb(facts), date, created["id"]))
                return created["id"]

            def approve(number, metadata, text, decision="rejected", severity=None, *,
                        draft=False, scenario=None, expected_initial=None):
                source = event(number, metadata, "2026-11-12T12:00:00Z", scenario)
                if expected_initial is None:
                    assert mock.run_once()
                else:
                    assert real.run_once()
                    initial = store.inspect_event("europe", source)
                    report["initial_reviews"].append(initial)
                    assert initial["review"] is not None
                    assert initial["review"]["result"]["decision"] == expected_initial
                memory.correct(source, "supervisor-europe", CorrectionRequest(
                    request_id=uuid4(), expected_version=0, decision=decision, severity=severity, reason=text))
                suggestion = memory.list_memory()["guidance"][0]
                if draft:
                    assert real.draft_once()
                    suggestion = memory.list_memory()["guidance"][0]
                    report["drafts"].append(suggestion)
                    assert suggestion["draft_status"] == "completed"
                    assert suggestion["draft_result"]["reusable"]
                    text = suggestion["draft_result"]["text"]
                return memory.approve(suggestion["id"], "supervisor-europe", GuidanceApproval(text=text))

            def stop_guidance():
                for row in memory.list_memory()["guidance"]:
                    if row["status"] == "approved":
                        memory.reject_or_retire(row["id"], "supervisor-europe", "retire")

            def check(name, number, metadata, expected, ids, date="2026-11-14T12:00:00Z", *, scenario=None, severity=None):
                target = event(number, metadata, date, scenario)
                assert real.run_once()
                saved = store.inspect_event("europe", target)
                report["cases"].append({"name": name, "expected": expected, "event": saved})
                args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
                assert saved["review"] is not None, f"{name}: provider did not complete; inspect saved trace"
                result = saved["review"]["result"]
                received = saved["attempts"][0]["context"]["memory"]["approved_guidance"]
                assert {row["id"] for row in received} == {str(value) for value in ids}
                assert result["decision"] == expected, f"{name}: {result['decision']} instead of {expected}"
                if severity is not None:
                    assert result["severity"] == severity
                print(f"{name}: {result['decision']} — {result['rationale']}", flush=True)

            range_rule = approve(1, {},
                "Standing site policy: this camera requires at least 5 metres separation. Confirm separations below 5 metres with high severity; dismiss at 5 metres or more.",
                "confirmed", "high", draft=True, scenario="distance_3m", expected_initial="rejected")
            check("distance_3m_after_correction", 1, {}, "confirmed", [range_rule["id"]], scenario="distance_3m", severity="high")
            check("distance_below_5m", 1, {}, "confirmed", [range_rule["id"]], scenario="distance_4_99m", severity="high")
            check("distance_exactly_5m", 1, {}, "rejected", [range_rule["id"]], scenario="distance_5m")
            stop_guidance()

            duration = approve(3, {"observed_duration_seconds": 95},
                "Standing site rule: dismiss pallet obstruction alerts lasting 120 seconds or less. Above 120 seconds, confirm with medium severity.")
            check("duration_inside_exception", 3, {"observed_duration_seconds": 100}, "rejected", [duration["id"]])
            check("duration_outside_exception", 3, {"observed_duration_seconds": 121}, "confirmed", [duration["id"]])
            stop_guidance()

            count = approve(1, {"people_count": 1},
                "Standing site rule: dismiss proximity alerts at this camera when people_count is zero or one. For two or more people, use the baseline distance rule.")
            check("changed_people_count", 1, {"people_count": 0, "estimated_distance_m": 1.6}, "rejected", [count["id"]])
            stop_guidance()

            dated = approve(2, {}, "On November 12, 2026 only, entry to this zone is unrestricted. Other dates still require authorization.",
                            draft=True, scenario="november_12", expected_initial="needs_input")
            check("dated_permission_same_day", 2, {}, "rejected", [dated["id"]], scenario="november_12")
            check("dated_permission_other_day", 2, {}, "needs_input", [dated["id"]], scenario="november_14")
            stop_guidance()

            personal = event(2, {}, scenario="november_14")
            assert mock.run_once()
            memory.correct(personal, "supervisor-europe", CorrectionRequest(
                request_id=uuid4(), expected_version=0, decision="rejected", severity=None,
                reason="Only this person was authorized for this particular maintenance visit. This does not authorize any other person or visit."))
            assert real.draft_once()
            personal_draft = memory.list_memory()["guidance"][0]
            report["drafts"].append(personal_draft)
            assert personal_draft["draft_status"] == "completed"
            assert personal_draft["draft_result"]["reusable"] is False
            assert personal_draft["draft_result"]["text"] is None

            above = approve(1, {"estimated_distance_m": 2.5},
                "Dismiss proximity alerts at this camera when the reported separation is greater than 2 metres.")
            below = approve(1, {"estimated_distance_m": 1.6},
                "Confirm proximity alerts at this camera with high severity when reported separation is 2 metres or less.",
                "confirmed", "high")
            check("different_outcomes_disjoint_conditions", 1, {"estimated_distance_m": 2.6}, "rejected", [above["id"], below["id"]])
            contradictory = approve(1, {"estimated_distance_m": 1.6},
                "Dismiss proximity alerts at this camera when reported separation is 2 metres or less.")
            check("applicable_rules_conflict", 1, {"estimated_distance_m": 1.7}, "needs_input",
                  [above["id"], below["id"], contradictory["id"]])
            report["passed"] = len(report["cases"])
    finally:
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
        with psycopg.connect(host=settings.pg_host, port=settings.pg_port, dbname="postgres",
                             user="review_bootstrap", autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


if __name__ == "__main__":
    main()
