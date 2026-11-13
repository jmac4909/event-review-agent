"""Build a fresh, keyless Mock stack; verify feedback and restart persistence.

Uses a unique Compose project and removes only its disposable volumes afterward.
The normal application project is never stopped or reset.
"""

import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import URLError
from urllib.request import Request, build_opener, HTTPCookieProcessor
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--port", type=int, default=8117)
    parser.add_argument("--builder", help="Optional preconfigured, resource-limited Buildx builder")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = f"review-check-{uuid4().hex[:10]}"
    source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"project": project, "source": str(source), "checks": {}}
    checks = report["checks"]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("REVIEW_", "ANTHROPIC_", "OPENAI_", "CODEX_"))}
    env["REVIEW_PORT"] = str(args.port)
    if args.builder:
        env["BUILDX_BUILDER"] = args.builder
    # Limits are for this acceptance stack, not application configuration.
    override = args.output / "limits.json"
    override.write_text(json.dumps({"services": {
        "db": {"mem_limit": "256m", "cpus": 0.5},
        "setup": {"mem_limit": "256m", "cpus": 0.5},
        "api": {"mem_limit": "384m", "cpus": 0.5},
        "workers": {"mem_limit": "768m", "cpus": 0.5},
    }}, indent=2) + "\n")
    compose = ["docker", "compose", "--project-name", project, "--env-file", "/dev/null",
               "-f", str(source / "compose.yaml"), "-f", str(override.resolve())]
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base = f"http://127.0.0.1:{args.port}"

    def request(path, body=None, method=None):
        data = None if body is None else json.dumps(body).encode()
        response = opener.open(Request(base + "/api" + path, data=data, method=method,
                               headers={"Content-Type": "application/json"}), timeout=10)
        raw = response.read()
        return json.loads(raw) if raw else None

    def wait_for(read, ready, label):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                result = read()
                if ready(result):
                    return result
            except (URLError, ConnectionError, TimeoutError):
                pass  # Readiness observation, never a repeated write.
            time.sleep(1)
        raise RuntimeError(f"Timed out waiting for {label}")

    def login(region):
        return request("/session", {"account_id": f"supervisor-{region}"})

    with (args.output / "compose.log").open("w") as log:
        def run(arguments):
            return subprocess.run(arguments, env=env, cwd=source, check=True, stdout=log, stderr=log)

        def output(arguments):
            return subprocess.check_output(arguments, env=env, cwd=source, text=True).strip()

        def snapshot():
            tables = ("events", "attempts", "reviews", "corrections", "guidance", "event_feed", "site_context")
            fields = [f"'{table}', (SELECT md5(coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text), '[]')::text) FROM review.{table} t)" for table in tables]
            query = "SELECT jsonb_build_object(" + ",".join(fields) + ")"
            return json.loads(output(compose + ["exec", "-T", "db", "psql", "-U", "review_bootstrap", "-d", "event_review", "-Atc", query]))

        before = output(["docker", "ps", "--filter", "label=com.docker.compose.project=event-review", "--format", "{{.ID}}"])
        assert not output(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.ID}}"])
        try:
            # No key or .env is supplied. Build from the specified source and use
            # new project volumes; this deliberately does not reuse live data.
            run(compose + ["up", "--build", "--detach", "--wait", "--wait-timeout", "120"])
            health = wait_for(lambda: request("/health"), lambda r: r["status"] == "ok", "API readiness")
            assert health["model_mode"] == "mock"
            checks["fresh_build_and_default_mock_without_key"] = True
            for region in ("europe", "us", "asia"):
                login(region)
                state = request("/feed")
                assert state["reviewer"] == "mock" and not any(p["enabled"] for p in state["providers"] if p["id"] != "mock")
                events = wait_for(lambda: request("/events"), lambda r: len(r["items"]) == 4 and all(e["assessment"] for e in r["items"]), f"{region} seed reviews")
                assert all(e["region_id"] == region for e in events["items"])
            checks["twelve_seed_reviews_all_regions_mock"] = True
            login("europe")
            event_id = "00000001-0000-4000-8000-000000000003"
            original = request(f"/events/{event_id}")["review"]
            assert original["result"]["severity"] == "medium"
            wording = "Site policy: Matching 95-second pallet obstructions at East exit are high severity. This is a standing rule for future matching events."
            correction = request(f"/events/{event_id}/corrections", {"request_id": str(uuid4()), "expected_version": 0,
                "decision": "confirmed", "severity": "high", "reason": wording})
            proposal = wait_for(lambda: request("/memory")["guidance"][0], lambda r: r["draft_status"] == "completed", "guidance draft")
            assert proposal["status"] == "proposed" and proposal["draft_result"]["reusable"]
            unapproved = request(f"/events/{event_id}/simulate", {"request_id": str(uuid4())})
            pending = wait_for(lambda: request(f"/events/{unapproved['id']}"), lambda r: r["review"] is not None, "unapproved-memory review")
            assert not pending["attempts"][0]["context"]["memory"]["approved_guidance"]
            approved = request(f"/guidance/{proposal['id']}/approve", {"text": wording})
            later = request(f"/events/{event_id}/simulate", {"request_id": str(uuid4())})
            after = wait_for(lambda: request(f"/events/{later['id']}"), lambda r: r["review"] is not None, "later review")
            assert after["review"]["result"]["severity"] == "high"
            assert after["attempts"][0]["context"]["memory"]["approved_guidance"][0]["id"] == approved["id"]
            assert request(f"/events/{event_id}")["review"] == original
            report["feedback"] = {"before": original, "correction": correction, "approved": approved, "after": after}
            checks["feedback_changes_later_review_original_preserved"] = True
            login("us")
            try:
                request(f"/events/{event_id}")
                raise AssertionError("Foreign event was exposed")
            except URLError as error:
                assert getattr(error, "code", None) == 404
            assert request("/memory") == {"corrections": [], "guidance": []}
            checks["cross_region_event_and_memory_isolation"] = True
            login("asia")
            scenario_events = []
            for scenario, number, distance, day in (
                ("november_12", 2, None, 12), ("november_14", 2, None, 14),
                ("distance_3m", 1, 3, None), ("distance_4_99m", 1, 4.99, None), ("distance_5m", 1, 5, None),
            ):
                source_event_id = f"00000003-0000-4000-8000-{number:012d}"
                body = {"request_id": str(uuid4()), "scenario": scenario}
                created = request(f"/events/{source_event_id}/simulate", body)
                assert request(f"/events/{source_event_id}/simulate", body)["id"] == created["id"]
                assert created["simulation_scenario"] == scenario
                if day is not None:
                    assert created["occurred_at"] == f"2026-11-{day}T12:00:00Z"
                else:
                    assert created["metadata"]["estimated_distance_m"] == distance
                saved_scenario = wait_for(lambda: request(f"/events/{created['id']}"), lambda r: r["review"] is not None, scenario)
                assert saved_scenario["review"]["result"]["decision"] == ("needs_input" if day else "rejected")
                scenario_events.append(saved_scenario)
            report["scenarios"] = scenario_events
            checks["explicit_scenarios_reviewed_by_normal_mock_workers"] = True
            login("europe")
            state = request("/feed")
            started = request("/feed", {"running": True, "reviewer": "mock", "expected_version": state["version"]}, "PUT")
            request("/feed", {"running": False, "reviewer": "mock", "expected_version": started["version"]}, "PUT")
            wait_for(lambda: request("/events"), lambda r: all(e["assessment"] for e in r["items"]), "queued reviews")
            # Model a database that previously used regional real providers.
            # Global Mock must ignore these preferences without deleting them.
            run(compose + ["exec", "-T", "db", "psql", "-U", "review_bootstrap", "-d", "event_review", "-c",
                "UPDATE review.event_feed SET selected_reviewer=CASE WHEN region_id='us' THEN 'anthropic' ELSE 'codex' END"])
            saved = snapshot()
            run(compose + ["down"])
            run(compose + ["up", "--detach", "--wait", "--wait-timeout", "120"])
            wait_for(lambda: request("/health"), lambda r: r["status"] == "ok", "restarted API")
            login("europe")
            assert snapshot() == saved
            assert request(f"/events/{later['id']}")["review"] == after["review"]
            login("asia")
            for saved_scenario in scenario_events:
                assert request(f"/events/{saved_scenario['id']}") == saved_scenario
            login("europe")
            checks["restart_preserves_all_seven_tables"] = True
            report["table_hashes"] = saved
            for index, region in enumerate(("europe", "us", "asia"), 1):
                login(region)
                state = request("/feed")
                assert state["model_selection"] == "global" and state["reviewer"] == "mock"
                try:
                    request("/reviewer", {"reviewer": "codex", "expected_version": state["version"]}, "PUT")
                    raise AssertionError("Regional choice overrode global mode")
                except URLError as error:
                    assert getattr(error, "code", None) == 422
                assert request("/feed") == state
                source_id = f"0000000{index}-0000-4000-8000-000000000001"
                new = request(f"/events/{source_id}/simulate", {"request_id": str(uuid4())})
                reviewed = wait_for(lambda: request(f"/events/{new['id']}"), lambda r: r["review"] is not None, f"{region} global Mock review")
                assert reviewed["attempts"][0]["reviewer_mode"] == "mock"
                assert reviewed["attempts"][0]["model_name"].startswith("deterministic-mock")
                correction_id = str(uuid4())
                request(f"/events/{source_id}/corrections", {"request_id": correction_id, "expected_version": 0,
                    "decision": "confirmed", "severity": "low", "reason": "Site policy: Matching proximity events have low severity. This is a standing rule."})
                draft = wait_for(lambda: next(g for g in request("/memory")["guidance"] if g["source_correction_id"] == correction_id),
                                 lambda g: g["draft_status"] == "completed", f"{region} global Mock suggestion")
                assert draft["draft_history"][0]["reviewer_mode"] == "mock"
                assert draft["draft_history"][0]["model_name"].startswith("deterministic-mock")
            choices = output(compose + ["exec", "-T", "db", "psql", "-U", "review_bootstrap", "-d", "event_review", "-Atc",
                "SELECT json_object_agg(region_id,selected_reviewer) FROM review.event_feed"])
            assert json.loads(choices) == {"europe": "codex", "us": "anthropic", "asia": "codex"}
            checks["global_mock_overrides_saved_real_choices_for_reviews_and_drafts"] = True
            checks["regional_preferences_preserved_and_selector_rejected"] = True
        finally:
            # Cleanup is restricted to the unique project created above.
            run(compose + ["down", "--volumes", "--remove-orphans"])
            if args.builder:
                run(["docker", "buildx", "stop", args.builder])
            checks["owned_stack_removed"] = not output(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.ID}}"])
            checks["live_application_containers_unchanged"] = before == output(["docker", "ps", "--filter", "label=com.docker.compose.project=event-review", "--format", "{{.ID}}"])
            (args.output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    assert all(checks.values())
    print(json.dumps({"project": project, "checks": checks, "evidence": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
