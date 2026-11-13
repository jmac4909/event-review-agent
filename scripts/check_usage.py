"""Estimate Anthropic cost from saved reports and/or the configured database.

No model calls are made. Prices are USD per million tokens, checked 2026-11-13:
https://platform.claude.com/docs/en/models/sonnet-5-5/overview
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys


def records(value):
    """Reports repeat snapshots; identify a call by model and start time."""
    if isinstance(value, dict):
        usage = value.get("usage")
        if isinstance(usage, dict) and usage.get("provider") == "anthropic":
            model = value.get("name") or value.get("model_name") or value.get("model")
            started = value.get("model_started_at") or value.get("started_at")
            if not model or not started:
                raise ValueError("Cannot identify an Anthropic call; inspect the source record.")
            if isinstance(started, str):
                started = datetime.fromisoformat(started.replace("Z", "+00:00"))
            yield {"model": model, "started_at": started.isoformat(), "usage": usage}
        for child in value.values():
            yield from records(child)
    elif isinstance(value, list):
        for child in value:
            yield from records(child)


def database_records():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/src"))
    from event_review.database import EventStore
    from event_review.settings import Settings

    store = EventStore(Settings.from_env())
    for region in ("europe", "us", "asia"):
        with store.for_region(region) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            for row in connection.execute("SELECT steps FROM review.attempts"):
                yield from records(row["steps"])
            for row in connection.execute("SELECT draft_history FROM review.guidance"):
                yield from records(row["draft_history"])


def estimate(call):
    usage = call["usage"]
    # This adapter does not request cache writes. Do not guess their TTL price
    # or apply this model's rates to another model.
    if call["model"] != "anthropic/claude-sonnet-5-5" or usage.get("cache_creation_input_tokens", 0):
        return None
    if "input_tokens" not in usage or "output_tokens" not in usage:
        return None
    return (Decimal(usage["input_tokens"]) * 2 + Decimal(usage["output_tokens"]) * 10
            + Decimal(usage.get("cache_read_input_tokens", 0)) / 10) / 1_000_000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="*", type=Path)
    parser.add_argument("--database", action="store_true", help="Include saved calls from all three scoped regions")
    parser.add_argument("--output", type=Path, help="Save the full call ledger as JSON")
    args = parser.parse_args()
    if not args.reports and not args.database:
        parser.error("Supply a report or --database")
    calls = {}
    sources = [list(records(json.loads(path.read_text()))) for path in args.reports]
    if args.database:
        sources.append(list(database_records()))
    for source in sources:
        for call in source:
            key = (call["model"], call["started_at"])
            if key in calls and calls[key] != call:
                raise ValueError("Conflicting usage for the same call; inspect the source records.")
            calls[key] = call
    total = Decimal(0)
    unpriced = 0
    for call in calls.values():
        cost = estimate(call)
        call["estimated_usd"] = None if cost is None else str(cost)
        if cost is None:
            unpriced += 1
        else:
            total += cost
    summary = {"as_of": datetime.now(timezone.utc).isoformat(), "calls": len(calls),
               "input_tokens": sum(c["usage"].get("input_tokens", 0) for c in calls.values()),
               "output_tokens": sum(c["usage"].get("output_tokens", 0) for c in calls.values()),
               "estimated_usd": str(total), "unpriced_calls": unpriced,
               "pricing_source": "https://platform.claude.com/docs/en/models/sonnet-5-5/overview",
               "rates_checked": "2026-11-13",
               "scope": "Recorded usage only, including failed verdicts. Not an invoice or account balance. "
                        "Calls without a recorded usage response and usage outside this project are not included."}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({**summary, "records": list(calls.values())}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
