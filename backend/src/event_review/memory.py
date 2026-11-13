"""Supervisor evidence and explicitly approved guidance, with scoped SQL."""

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .database import EventNotFound, EventStore, REGIONS
from .models import CorrectionRequest, Event, GuidanceApproval, validate_verdict
from .retrieval import MAX_CANDIDATES, MAX_GUIDANCE, MAX_SELECTED, TEXT_LIMIT, rank_memory
from .settings import Settings
from .simulation import scenario_evidence


class FeedbackConflict(ValueError):
    pass


class InvalidFeedback(ValueError):
    pass


def guidance_metadata(metadata: dict) -> dict:
    # Legacy column stores source facts for inspection, never eligibility.
    return {key: value for key, value in metadata.items() if key != "detector_score"}


class MemoryStore:
    def __init__(self, settings: Settings, region: str):
        if region not in REGIONS:
            raise ValueError("A known region is required.")
        self.region = region
        self.events = EventStore(settings)

    def correct(self, event_id: UUID, supervisor_id: str, body: CorrectionRequest) -> dict:
        reason = body.reason.strip()
        try:
            validate_verdict({"decision": body.decision, "severity": body.severity,
                              "rationale": reason, "question": None})
        except ValueError:
            raise InvalidFeedback("Enter a nonblank reason. Confirmed needs severity; rejected has none.") from None
        with self.events.for_region(self.region) as connection:
            event = self.events.lock_event(connection, self.region, event_id)
            previous = connection.execute("""
                SELECT * FROM review.corrections WHERE id = %s AND region_id = %s
            """, (body.request_id, self.region)).fetchone()
            if previous is not None:
                if (previous["event_id"] == event_id and previous["supervisor_id"] == supervisor_id
                        and previous["version"] == body.expected_version + 1
                        and previous["decision"] == body.decision and previous["severity"] == body.severity
                        and previous["reason"] == reason):
                    return previous
                raise FeedbackConflict("This request ID already saved different feedback.")
            review = connection.execute("""
                SELECT event_id FROM review.reviews WHERE event_id = %s AND region_id = %s
            """, (event_id, self.region)).fetchone()
            if review is None:
                raise FeedbackConflict("Wait for an agent review before submitting supervisor feedback.")
            version = connection.execute("""
                SELECT coalesce(max(version), 0) AS version FROM review.corrections
                WHERE event_id = %s AND region_id = %s
            """, (event_id, self.region)).fetchone()["version"]
            if version != body.expected_version:
                raise FeedbackConflict("Another supervisor update was saved. Reload the latest review before saving your changes.")
            snapshot = Event.model_validate(event).model_dump(mode="json", exclude={"status", "simulation_scenario"})
            correction = connection.execute("""
                INSERT INTO review.corrections
                    (id, event_id, region_id, version, decision, severity, reason, supervisor_id, event_snapshot)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING RETURNING *
            """, (body.request_id, event_id, self.region, version + 1, body.decision,
                  body.severity, reason, supervisor_id, Jsonb(snapshot))).fetchone()
            if correction is None:
                raise FeedbackConflict("This request ID has already been used. Reload before trying again.")
            # A newer source correction invalidates old derived guidance.
            connection.execute("""
                UPDATE review.guidance SET status = 'superseded', retired_at = clock_timestamp(), retired_by = %s,
                    draft_status = CASE WHEN draft_status IN ('queued', 'running') THEN 'cancelled' ELSE draft_status END
                WHERE source_event_id = %s AND region_id = %s AND status IN ('proposed', 'approved')
            """, (supervisor_id, event_id, self.region))
            connection.execute("""
                INSERT INTO review.guidance
                    (id, region_id, source_event_id, source_correction_id, event_type, site, zone, camera,
                     metadata_condition, proposed_text, decision, severity, draft_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued')
            """, (uuid4(), self.region, event_id, correction["id"], event["event_type"], event["site"],
                  event["zone"], event["camera"], Jsonb(guidance_metadata(event["metadata"])), reason, body.decision, body.severity))
            connection.execute("""
                UPDATE review.events SET status = 'reviewed' WHERE id = %s AND region_id = %s
            """, (event_id, self.region))
            return correction

    def approve(self, guidance_id: UUID, supervisor_id: str, body: GuidanceApproval) -> dict:
        wording = body.text.strip()
        # A basic guard against acknowledgements, not a semantic policy check.
        # The supervisor remains responsible for the rule's meaning.
        if len(wording) < 20 or len(wording.split()) < 4:
            raise InvalidFeedback("Write a reusable rule: when it applies and what future reviews should do.")
        with self.events.for_region(self.region) as connection:
            guidance = self._lock_guidance(connection, guidance_id)
            # Approval authorizes reuse. The immutable correction owns the
            # classification; neither the browser nor draft can change it here.
            source = connection.execute("""
                SELECT decision, severity FROM review.corrections
                WHERE id = %s AND region_id = %s
            """, (guidance["source_correction_id"], self.region)).fetchone()
            if guidance["status"] == "approved":
                if (guidance["approved_text"] == wording and guidance["decision"] == source["decision"]
                        and guidance["severity"] == source["severity"] and guidance["decided_by"] == supervisor_id):
                    return guidance
                raise FeedbackConflict("This guidance was already approved. Retire it before replacing it.")
            if guidance["status"] != "proposed":
                raise FeedbackConflict("This suggestion is no longer awaiting approval. Refresh its current status.")
            return connection.execute("""
                UPDATE review.guidance
                SET status = 'approved', approved_text = %s, decision = %s, severity = %s,
                    decided_at = clock_timestamp(), decided_by = %s,
                    draft_status = CASE WHEN draft_status IN ('queued', 'running') THEN 'cancelled' ELSE draft_status END
                WHERE id = %s AND region_id = %s RETURNING *
            """, (wording, source["decision"], source["severity"], supervisor_id, guidance_id, self.region)).fetchone()

    def reject_or_retire(self, guidance_id: UUID, supervisor_id: str, action: str) -> dict:
        if action not in ("reject", "retire"):
            raise ValueError("Unknown guidance action.")
        expected, status = ("proposed", "rejected") if action == "reject" else ("approved", "retired")
        with self.events.for_region(self.region) as connection:
            guidance = self._lock_guidance(connection, guidance_id)
            if guidance["status"] == status:
                return guidance
            if guidance["status"] != expected:
                raise FeedbackConflict("This guidance changed. Refresh its current status.")
            if action == "reject":
                return connection.execute("""
                    UPDATE review.guidance SET status = 'rejected', decided_at = clock_timestamp(), decided_by = %s,
                        draft_status = CASE WHEN draft_status IN ('queued', 'running') THEN 'cancelled' ELSE draft_status END
                    WHERE id = %s AND region_id = %s RETURNING *
                """, (supervisor_id, guidance_id, self.region)).fetchone()
            return connection.execute("""
                UPDATE review.guidance SET status = 'retired', retired_at = clock_timestamp(), retired_by = %s
                WHERE id = %s AND region_id = %s RETURNING *
            """, (supervisor_id, guidance_id, self.region)).fetchone()

    def list_memory(self) -> dict:
        with self.events.for_region(self.region) as connection:
            corrections = connection.execute("""
                SELECT * FROM review.corrections WHERE region_id = %s
                ORDER BY created_at DESC, id LIMIT 100
            """, (self.region,)).fetchall()
            guidance = connection.execute("""
                SELECT g.*, c.decision AS source_decision, c.severity AS source_severity
                FROM review.guidance g
                JOIN review.corrections c ON c.id = g.source_correction_id AND c.region_id = g.region_id
                WHERE g.region_id = %s
                ORDER BY g.created_at DESC, g.id LIMIT 100
            """, (self.region,)).fetchall()
            return {"corrections": corrections, "guidance": guidance}

    def select_for(self, event: dict, encoder=None) -> dict:
        if event["region_id"] != self.region:
            raise ValueError("Memory must match the worker's region.")
        with self.events.for_region(self.region) as connection:
            # Scope and trust are enforced before text goes to the encoder.
            # One extra row signals that the bounded candidate set is incomplete.
            row = connection.execute("""
                WITH recent_cases AS (
                    SELECT DISTINCT ON (event_id) id, event_id, version, decision, severity,
                        reason, supervisor_id, created_at, event_snapshot
                    FROM review.corrections
                    WHERE region_id = %s AND event_id <> %s
                      AND event_snapshot->>'event_type' = %s AND event_snapshot->>'site' = %s
                      AND event_snapshot->>'zone' = %s AND event_snapshot->>'camera' = %s
                    ORDER BY event_id, version DESC
                ), selected_cases AS (
                    -- Filter after choosing the latest version, so older corrections cannot return.
                    SELECT * FROM recent_cases c
                    WHERE NOT EXISTS (
                        SELECT 1 FROM review.guidance g
                        WHERE g.region_id = %s AND g.source_correction_id = c.id AND g.status = 'retired'
                    )
                    ORDER BY created_at DESC, id LIMIT %s
                ), selected_guidance AS (
                    SELECT g.id, g.source_event_id, g.source_correction_id, g.decision, g.severity,
                        g.approved_text AS text, g.decided_at, g.decided_by, g.event_type,
                        g.site, g.zone, g.camera,
                        c.event_snapshot
                    FROM review.guidance g
                    JOIN review.corrections c ON c.id = g.source_correction_id AND c.region_id = g.region_id
                    WHERE g.region_id = %s AND g.status = 'approved'
                      AND g.event_type = %s AND g.site = %s AND g.zone = %s AND g.camera = %s
                    ORDER BY g.decided_at DESC, g.id LIMIT %s
                )
                SELECT coalesce((SELECT jsonb_agg(c) FROM selected_cases c), '[]') AS corrections,
                       coalesce((SELECT jsonb_agg(g) FROM selected_guidance g), '[]') AS approved_guidance
            """, (self.region, event["id"], event["event_type"], event["site"], event["zone"], event["camera"], self.region, MAX_CANDIDATES + 1,
                  self.region, event["event_type"], event["site"], event["zone"], event["camera"], MAX_GUIDANCE + 1)).fetchone()
        # Ranking runs after the transaction closes; no model work holds a lock.
        cases, case_scores = rank_memory(event, row["corrections"][:MAX_CANDIDATES], encoder)
        # Include every approved rule in this scope, not only the closest three.
        # The model checks written conditions; differing case outcomes may be
        # compatible rules for different distances, dates or circumstances.
        guidance, guidance_scores = rank_memory(event, row["approved_guidance"], encoder, limit=MAX_GUIDANCE)
        return {"corrections": cases, "approved_guidance": guidance,
                "guidance_overflow": len(row["approved_guidance"]) > MAX_GUIDANCE,
                "selection": {
                    "method": "hybrid" if encoder is not None else "lexical",
                    "model": encoder.model_name if encoder is not None else None,
                    "model_revision": encoder.revision if encoder is not None else None,
                    "weights": {"lexical": 0.5, "semantic": 0.5} if encoder is not None else {"lexical": 1.0},
                    "scope": "Region, type, site, zone and camera. Event measurements do not filter guidance; the model checks each rule's conditions.",
                    "text_limit_characters": TEXT_LIMIT, "embedding_token_limit": 512 if encoder is not None else None,
                    "candidate_limit": MAX_CANDIDATES, "more_cases_available": len(row["corrections"]) > MAX_CANDIDATES,
                    "case_limit": MAX_SELECTED, "guidance_limit": MAX_GUIDANCE,
                    "case_scores": case_scores, "guidance_scores": guidance_scores,
                }}

    def simulate(self, event_id: UUID, request_id: UUID, scenario: str = "repeat") -> dict:
        with self.events.for_region(self.region) as connection:
            source = self.events.lock_event(connection, self.region, event_id)
            existing = connection.execute("""
                SELECT * FROM review.events WHERE id = %s AND region_id = %s
            """, (request_id, self.region)).fetchone()
            if existing is not None:
                # Older simulations have no scenario recorded; they were repeats.
                if (existing["simulated_from_event_id"] == event_id
                        and (existing["simulation_scenario"] or "repeat") == scenario):
                    return existing
                raise FeedbackConflict("This request ID already belongs to a different event or scenario.")
            evidence = scenario_evidence(source, scenario)
            title = source["title"].removesuffix(" (follow-up)") + " (follow-up)"
            result = connection.execute("""
                INSERT INTO review.events
                    (id, region_id, source_ref, event_type, title, description, site, zone, camera,
                     occurred_at, metadata, simulated_from_event_id, simulation_scenario)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp()), %s, %s, %s)
                ON CONFLICT DO NOTHING RETURNING *
            """, (request_id, self.region, f"simulation-{request_id}", source["event_type"], title,
                  evidence["description"], source["site"], source["zone"], source["camera"],
                  evidence["occurred_at"], Jsonb(evidence["metadata"]), event_id, scenario)).fetchone()
            if result is None:
                raise FeedbackConflict("This request ID has already been used. Reload before trying again.")
            return result

    def _lock_guidance(self, connection, guidance_id: UUID) -> dict:
        source = connection.execute("""
            SELECT source_event_id FROM review.guidance WHERE id = %s AND region_id = %s
        """, (guidance_id, self.region)).fetchone()
        if source is None:
            raise EventNotFound("Guidance not found.")
        # Same lock order as correction saves: event, then its derived guidance.
        self.events.lock_event(connection, self.region, source["source_event_id"])
        return connection.execute("""
            SELECT * FROM review.guidance WHERE id = %s AND region_id = %s FOR UPDATE
        """, (guidance_id, self.region)).fetchone()
