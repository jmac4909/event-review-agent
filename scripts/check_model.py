"""Exercise a provider and feedback with synthetic data in a disposable database."""

import argparse
from dataclasses import replace
import json
import sys
from uuid import UUID, uuid4

import psycopg
from psycopg import sql

from dev import ROOT, SOCKET, PORT, start_database

sys.path.insert(0, str(ROOT / "backend/src"))
from event_review.bootstrap import bootstrap
from event_review.database import EventStore
from event_review.memory import MemoryStore
from event_review.models import CorrectionRequest, GuidanceApproval
from event_review.settings import Settings
from event_review.worker import Worker


EXAMPLES = {
    "exit-severity": {
        "source_id": "00000001-0000-4000-8000-000000000003",
        "before_severity": "medium", "after_severity": "high",
        "reason": "Site policy: Matching 95-second pallet obstructions at East exit are high severity. "
                  "This is a standing rule.",
    },
    "proximity-severity": {
        "source_id": "00000001-0000-4000-8000-000000000001",
        "before_severity": "high", "after_severity": "medium",
        "reason": "Site policy: Matching 1.4 m proximity alerts at Dock 3 are medium severity. "
                  "This is a standing rule.",
    },
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("mock", "anthropic", "codex"), default="mock")
    parser.add_argument("--example", choices=tuple(EXAMPLES), default="exit-severity")
    args = parser.parse_args()
    example = EXAMPLES[args.example]
    database = f"review_smoke_{uuid4().hex[:12]}"
    settings = replace(Settings.from_env(), pg_host=str(SOCKET), pg_port=PORT,
                       pg_database=database, model_mode=args.mode)
    worker = Worker(settings, "europe", "provider-check")  # Fail on missing credentials before creating data.
    start_database()
    bootstrap(str(SOCKET), PORT, database)
    source_id = UUID(example["source_id"])
    destination = ROOT / ".work" / database / "reviews.json"
    destination.parent.mkdir(parents=True)
    report = {"mode": args.mode, "example": args.example}
    try:
        with psycopg.connect(host=str(SOCKET), port=PORT, user="review_bootstrap", dbname=database) as connection:
            connection.execute("DELETE FROM review.events WHERE id <> %s", (source_id,))
        store = EventStore(settings)
        memory = MemoryStore(settings, "europe")
        worker.run_once()
        before = store.inspect_event("europe", source_id)
        report["before"] = before
        if before["review"] is None:
            raise RuntimeError(before["attempts"][0]["error"] or "Provider did not finish the first review.")
        assert before["review"]["result"]["decision"] == "confirmed", "Inspect the baseline decision before continuing."
        assert before["review"]["result"]["severity"] == example["before_severity"], "Inspect the baseline severity before continuing."
        correction = memory.correct(source_id, "supervisor-europe", CorrectionRequest(
            request_id=uuid4(), expected_version=0, decision="confirmed", severity=example["after_severity"],
            reason=example["reason"],
        ))
        worker.draft_once()
        suggestion = memory.list_memory()["guidance"][0]
        report.update(correction=correction, suggestion=suggestion)
        if suggestion["draft_status"] != "completed":
            raise RuntimeError("The guidance draft did not finish. Inspect the provider configuration.")
        proposal = suggestion["draft_result"]
        if not proposal["reusable"]:
            raise RuntimeError("The model considered this correction case-specific; review its rationale before approving.")
        # This scripted human approval is a test fixture, not model self-approval.
        approved = memory.approve(suggestion["id"], "supervisor-europe", GuidanceApproval(
            text=proposal["text"],
        ))
        report["approved_guidance"] = approved
        later = memory.simulate(source_id, uuid4())
        worker.run_once()
        after = store.inspect_event("europe", later["id"])
        report["after"] = after
        if after["review"] is None:
            raise RuntimeError(after["attempts"][0]["error"] or "Provider did not finish the later review.")
        print(f"Before: confirmed/{before['review']['result']['severity']}; "
              f"after approval: {after['review']['result']['decision']}/{after['review']['result']['severity']}")
        print(f"Exact application contexts and results: {destination}")
        assert after["review"]["result"]["decision"] == "confirmed", "The later decision changed unexpectedly. Inspect the report."
        assert after["review"]["result"]["severity"] == example["after_severity"], "The later severity did not follow applicable approved guidance. Inspect the report."
        assert store.inspect_event("europe", source_id)["review"] == before["review"], "The original review changed."
    finally:
        # Preserve partial evidence even if a provider refuses to generalize.
        destination.write_text(json.dumps(report, indent=2, default=str) + "\n")
        with psycopg.connect(host=str(SOCKET), port=PORT, user="review_bootstrap", dbname="postgres", autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


if __name__ == "__main__":
    main()
