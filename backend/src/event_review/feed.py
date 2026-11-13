"""A continuing synthetic source, coordinated by one database row per region."""

from datetime import timedelta
import json
from uuid import uuid4

from psycopg.types.json import Jsonb

from .database import EventStore, REGIONS
from .models import FeedRequest, ReviewerRequest
from .settings import ROOT, Settings


MAX_UNRESOLVED = 20


class FeedConflict(ValueError):
    pass


class ReviewerUnavailable(ValueError):
    pass


class FeedStore:
    def __init__(self, settings: Settings, region: str):
        if region not in REGIONS:
            raise ValueError("A known region is required.")
        self.region = region
        self.settings = settings
        self.events = EventStore(settings)
        self.templates = [event for event in json.loads((ROOT / "data/events.json").read_text())
                          if event["region_id"] == region]

    def get(self) -> dict:
        with self.events.for_region(self.region) as connection:
            row = connection.execute("SELECT * FROM review.event_feed WHERE region_id = %s", (self.region,)).fetchone()
            return self._view(connection, row)

    def set_running(self, supervisor_id: str, body: FeedRequest) -> dict:
        with self.events.for_region(self.region) as connection:
            row = self._lock(connection)
            reviewer = self.settings.reviewer_for(row["selected_reviewer"])
            if body.running and reviewer not in self.settings.available_models:
                raise ReviewerUnavailable("This model is not enabled on the server. Choose an available model.")
            if body.running and body.reviewer != reviewer:
                raise FeedConflict("The reviewer changed. Refresh before starting the stream.")
            now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            same = row["enabled"] == body.running and (not body.running or (
                row["model_mode"] == reviewer and row["run_seconds"] == body.run_seconds
                and (row["stops_at"] is None or row["stops_at"] > now)))
            if body.expected_version != row["version"]:
                # Repeating the immediately preceding desired-state change is
                # safe after a lost acknowledgement. Older changes are stale.
                if row["version"] == body.expected_version + 1 and same:
                    return self._view(connection, row)
                raise FeedConflict("The stream controls changed. Refresh before trying again.")
            if not same:
                stops_at = now + timedelta(seconds=body.run_seconds) if body.running and body.run_seconds else None
                row = connection.execute("""
                    UPDATE review.event_feed
                    SET enabled = %s, model_mode = %s, version = version + 1,
                        next_event_at = CASE WHEN %s THEN clock_timestamp() ELSE next_event_at END,
                        run_seconds = CASE WHEN %s THEN %s ELSE run_seconds END, stops_at = %s,
                        updated_by = %s, updated_at = clock_timestamp()
                    WHERE region_id = %s RETURNING *
                """, (body.running, reviewer, body.running, body.running, body.run_seconds, stops_at,
                      supervisor_id, self.region)).fetchone()
            return self._view(connection, row)

    def set_reviewer(self, supervisor_id: str, body: ReviewerRequest) -> dict:
        if self.settings.model_mode != "regional":
            raise ReviewerUnavailable("The server sets the model for all regions.")
        if body.reviewer not in self.settings.available_models:
            raise ReviewerUnavailable("This model is not enabled on the server. Choose an available model.")
        with self.events.for_region(self.region) as connection:
            row = self._lock(connection)
            same = self.settings.reviewer_for(row["selected_reviewer"]) == body.reviewer
            if body.expected_version != row["version"]:
                if row["version"] == body.expected_version + 1 and same and not row["enabled"]:
                    return self._view(connection, row)  # Lost save acknowledgement.
                raise FeedConflict("The model or stream controls changed. Review the latest setting before saving.")
            if not same:
                row = connection.execute("""
                    UPDATE review.event_feed
                    SET selected_reviewer = %s, enabled = false, model_mode = NULL, stops_at = NULL,
                        version = version + 1, updated_by = %s, updated_at = clock_timestamp()
                    WHERE region_id = %s RETURNING *
                """, (body.reviewer, supervisor_id, self.region)).fetchone()
            return self._view(connection, row)

    def ingest_due(self) -> dict | None:
        with self.events.for_region(self.region) as connection:
            row = self._lock(connection)
            if not row["enabled"]:
                return None
            now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            if row["stops_at"] is not None and row["stops_at"] <= now:
                connection.execute("""
                    UPDATE review.event_feed SET enabled = false, version = version + 1,
                        updated_by = 'auto-stop', updated_at = clock_timestamp()
                    WHERE region_id = %s
                """, (self.region,))
                return None
            reviewer = self.settings.reviewer_for(row["selected_reviewer"])
            if row["model_mode"] != reviewer or reviewer not in self.settings.available_models:
                # Persist the stop so switching back does not silently resume
                # an old stream after a worker observes a reviewer change.
                connection.execute("""
                    UPDATE review.event_feed SET enabled = false, stops_at = NULL, version = version + 1,
                        updated_by = 'reviewer-change', updated_at = clock_timestamp()
                    WHERE region_id = %s
                """, (self.region,))
                return None
            if row["next_event_at"] > now or self._unresolved(connection) >= MAX_UNRESOLVED:
                return None
            template = self.templates[row["generated"] % len(self.templates)]
            sequence = row["generated"] + 1
            event = connection.execute("""
                INSERT INTO review.events
                    (id, region_id, source_ref, event_type, title, description, site, zone,
                     camera, occurred_at, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
            """, (uuid4(), self.region, f"demo-stream-{sequence}", template["event_type"], template["title"],
                  template["description"], template["site"], template["zone"], template["camera"], now,
                  Jsonb(template["metadata"]))).fetchone()
            # Event and cursor commit together. After downtime, emit one event
            # and schedule from now instead of replaying every missed interval.
            connection.execute("""
                UPDATE review.event_feed
                SET generated = %s, last_event_id = %s, last_event_at = %s, next_event_at = %s
                WHERE region_id = %s
            """, (sequence, event["id"], now, now + timedelta(seconds=self.settings.feed_interval_seconds), self.region))
            return event

    def _lock(self, connection):
        return connection.execute("SELECT * FROM review.event_feed WHERE region_id = %s FOR UPDATE", (self.region,)).fetchone()

    def _unresolved(self, connection) -> int:
        return connection.execute("""
            SELECT count(*) AS n FROM review.events WHERE region_id = %s
            AND status IN ('available', 'claimed', 'needs_input', 'failed')
        """, (self.region,)).fetchone()["n"]

    def _view(self, connection, row) -> dict:
        unresolved = self._unresolved(connection)
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        status = "running"
        reviewer = self.settings.reviewer_for(row["selected_reviewer"])
        if reviewer not in self.settings.available_models:
            status = "reviewer_unavailable"
        elif row["model_mode"] is not None and row["model_mode"] != reviewer:
            status = "reviewer_changed"
        elif row["stops_at"] is not None and row["stops_at"] <= now:
            status = "auto_stopped"
        elif not row["enabled"]:
            status = "paused"
        elif unresolved >= MAX_UNRESOLVED:
            status = "waiting_for_reviews"
        elif now > row["next_event_at"] + timedelta(seconds=max(5, self.settings.feed_interval_seconds)):
            status = "waiting_for_worker"
        return {"region_id": self.region, "status": status, "version": row["version"],
                "reviewer": reviewer, "model_selection": "regional" if self.settings.model_mode == "regional" else "global",
                "providers": self.provider_options(), "interval_seconds": self.settings.feed_interval_seconds,
                "run_seconds": row["run_seconds"], "stops_at": row["stops_at"],
                "unresolved": unresolved, "unresolved_limit": MAX_UNRESOLVED, "generated": row["generated"],
                "last_event_id": row["last_event_id"], "last_event_at": row["last_event_at"],
                "updated_by": row["updated_by"], "updated_at": row["updated_at"]}

    def provider_options(self) -> list[dict]:
        details = {
            "mock": ("Mock", "Sample responses; no model calls."),
            "anthropic": ("Claude", f"Claude API · {self.settings.anthropic_model}"),
            "codex": ("Codex", "AI reviews with Codex."),
        }
        return [{"id": mode, "label": label, "enabled": mode in self.settings.available_models,
                 "detail": detail if mode in self.settings.available_models else "Unavailable in this demo."}
                for mode, (label, detail) in details.items()]
