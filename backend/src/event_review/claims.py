"""Short database transactions for claims; do the review outside this class."""

from datetime import timedelta
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .database import EventStore, REGIONS
from .settings import Settings


MAX_ATTEMPTS = 3  # First attempt plus two automatic retries.


class ClaimConflict(ValueError):
    pass


class ClaimStore:
    def __init__(self, settings: Settings, region: str):
        # A worker gets this region from trusted configuration, not model output.
        if region not in REGIONS:
            raise ValueError("A known region is required.")
        self.region = region
        self.claim_seconds = settings.claim_seconds
        self.settings = settings
        self.available_models = settings.available_models
        self.events = EventStore(settings)

    def claim_next(self, worker_id: str) -> dict | None:
        if not worker_id.strip() or len(worker_id) > 100:
            raise ValueError("Worker ID must contain 1 to 100 characters.")

        while True:
            with self.events.for_region(self.region) as connection:
                event = connection.execute("""
                    SELECT e.id, e.attempts_used, e.active_attempt_id,
                        (SELECT selected_reviewer FROM review.event_feed
                         WHERE region_id = e.region_id) AS selected_reviewer
                    FROM review.events e
                    LEFT JOIN review.attempts a ON a.id = e.active_attempt_id
                    WHERE e.region_id = %s
                      AND (e.status = 'available'
                           OR (e.status = 'claimed' AND a.expires_at <= clock_timestamp()))
                    ORDER BY e.occurred_at, e.id
                    LIMIT 1
                    FOR UPDATE OF e SKIP LOCKED
                """, (self.region,)).fetchone()
                if event is None:
                    return None
                event["reviewer_mode"] = self.settings.reviewer_for(event["selected_reviewer"])
                if event["reviewer_mode"] not in self.available_models:
                    return None  # Wait for setup/a new selection without using an attempt.

                if event["active_attempt_id"] is not None:
                    expired = connection.execute("""
                        UPDATE review.attempts
                        SET status = 'expired', finished_at = clock_timestamp(),
                            error = 'Claim deadline passed.'
                        WHERE id = %s AND region_id = %s AND status = 'running'
                          AND expires_at <= clock_timestamp()
                        RETURNING id
                    """, (event["active_attempt_id"], self.region)).fetchone()
                    if expired is None:
                        # Another transaction may have changed the candidate
                        # between our SELECT snapshot and obtaining its lock.
                        continue

                if event["attempts_used"] >= MAX_ATTEMPTS:
                    connection.execute("""
                        UPDATE review.events SET status = 'failed', active_attempt_id = NULL
                        WHERE id = %s AND region_id = %s
                    """, (event["id"], self.region))
                    # Commit this exhausted event, then look for another one.
                    continue

                now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
                attempt = connection.execute("""
                    INSERT INTO review.attempts
                        (id, event_id, region_id, worker_id, started_at, expires_at, status, reviewer_mode)
                    VALUES (%s, %s, %s, %s, %s, %s, 'running', %s)
                    RETURNING *
                """, (uuid4(), event["id"], self.region, worker_id, now,
                      now + timedelta(seconds=self.claim_seconds), event["reviewer_mode"])).fetchone()
                connection.execute("""
                    UPDATE review.events
                    SET status = 'claimed', active_attempt_id = %s, attempts_used = attempts_used + 1
                    WHERE id = %s AND region_id = %s
                """, (attempt["id"], event["id"], self.region))
                return attempt

    def complete(self, event_id: UUID, attempt_id: UUID, worker_id: str, result: dict) -> dict:
        with self.events.for_region(self.region) as connection:
            event = self.events.lock_event(connection, self.region, event_id)
            attempt = self._get_attempt(connection, event_id, attempt_id, worker_id)

            if attempt["status"] == "completed":
                # A lost HTTP/worker acknowledgement may be retried after expiry.
                review = connection.execute("""
                    SELECT *, result = %s AS same_result FROM review.reviews
                    WHERE event_id = %s AND region_id = %s AND attempt_id = %s
                """, (Jsonb(result), event_id, self.region, attempt_id)).fetchone()
                if review is None or not review.pop("same_result"):
                    raise ClaimConflict("This attempt already saved a different result.")
                return review

            self._check_owner(event, attempt)
            saved = connection.execute("""
                UPDATE review.attempts SET status = 'completed', finished_at = clock_timestamp()
                WHERE id = %s AND region_id = %s AND status = 'running'
                  AND expires_at > clock_timestamp()
                RETURNING id
            """, (attempt_id, self.region)).fetchone()
            if saved is None:
                raise ClaimConflict("The claim expired; its result cannot be saved.")

            review = connection.execute("""
                INSERT INTO review.reviews (event_id, region_id, attempt_id, result)
                VALUES (%s, %s, %s, %s) RETURNING *
            """, (event_id, self.region, attempt_id, Jsonb(result))).fetchone()
            status = "needs_input" if result.get("decision") == "needs_input" else "reviewed"
            connection.execute("""
                UPDATE review.events SET status = %s, active_attempt_id = NULL
                WHERE id = %s AND region_id = %s
            """, (status, event_id, self.region))
            return review

    def start_review(self, event_id: UUID, attempt_id: UUID, worker_id: str,
                     context: dict, model_name: str) -> None:
        with self.events.for_region(self.region) as connection:
            event = self.events.lock_event(connection, self.region, event_id)
            attempt = self._get_attempt(connection, event_id, attempt_id, worker_id)
            self._check_owner(event, attempt)
            recorded = connection.execute("""
                UPDATE review.attempts
                SET context = %s, model_name = %s, model_started_at = clock_timestamp()
                WHERE id = %s AND region_id = %s AND expires_at > clock_timestamp()
                  AND model_started_at IS NULL
                RETURNING id
            """, (Jsonb(context), model_name, attempt_id, self.region)).fetchone()
            if recorded is None:
                raise ClaimConflict("The claim expired or already started its review.")

    def save_output(self, event_id: UUID, attempt_id: UUID, worker_id: str, output) -> None:
        with self.events.for_region(self.region) as connection:
            event = self.events.lock_event(connection, self.region, event_id)
            attempt = self._get_attempt(connection, event_id, attempt_id, worker_id)
            self._check_owner(event, attempt)
            recorded = connection.execute("""
                UPDATE review.attempts
                SET model_output = %s, model_finished_at = clock_timestamp()
                WHERE id = %s AND region_id = %s AND expires_at > clock_timestamp()
                  AND model_started_at IS NOT NULL AND model_finished_at IS NULL
                RETURNING id
            """, (Jsonb(output), attempt_id, self.region)).fetchone()
            if recorded is None:
                raise ClaimConflict("The claim expired or cannot record another output.")

    def start_step(self, event_id: UUID, attempt_id: UUID, worker_id: str,
                   kind: str, name: str, supplied_input: dict) -> int:
        with self.events.for_region(self.region) as connection:
            event = self.events.lock_event(connection, self.region, event_id)
            attempt = self._get_attempt(connection, event_id, attempt_id, worker_id)
            self._check_owner(event, attempt)
            recorded = connection.execute("""
                UPDATE review.attempts
                SET steps = steps || jsonb_build_array(jsonb_build_object(
                    'kind', %s::text, 'name', %s::text, 'input', %s::jsonb,
                    'started_at', clock_timestamp(), 'finished_at', NULL,
                    'output', NULL, 'error', NULL, 'usage', NULL))
                WHERE id = %s AND region_id = %s AND expires_at > clock_timestamp()
                  AND model_started_at IS NOT NULL AND model_finished_at IS NULL
                  AND jsonb_array_length(steps) < 7
                  AND (steps = '[]' OR steps -> -1 ->> 'finished_at' IS NOT NULL)
                RETURNING jsonb_array_length(steps) - 1 AS number
            """, (kind, name, Jsonb(supplied_input), attempt_id, self.region)).fetchone()
            if recorded is None:
                raise ClaimConflict("The claim expired or cannot start another step.")
            return recorded["number"]

    def finish_step(self, event_id: UUID, attempt_id: UUID, worker_id: str,
                    number: int, output=None, error: str | None = None, usage: dict | None = None) -> None:
        with self.events.for_region(self.region) as connection:
            event = self.events.lock_event(connection, self.region, event_id)
            attempt = self._get_attempt(connection, event_id, attempt_id, worker_id)
            self._check_owner(event, attempt)
            recorded = connection.execute("""
                UPDATE review.attempts
                SET steps = jsonb_set(steps, ARRAY[%s::text], (steps -> -1) || jsonb_build_object(
                    'output', %s::jsonb, 'error', %s::text, 'usage', %s::jsonb,
                    'finished_at', clock_timestamp()))
                WHERE id = %s AND region_id = %s AND expires_at > clock_timestamp()
                  AND jsonb_array_length(steps) = %s + 1
                  AND steps -> -1 ->> 'finished_at' IS NULL
                RETURNING id
            """, (str(number), Jsonb(output), error, Jsonb(usage), attempt_id, self.region, number)).fetchone()
            if recorded is None:
                raise ClaimConflict("The claim expired or this step already finished.")

    def fail(self, event_id: UUID, attempt_id: UUID, worker_id: str, error: str) -> dict:
        with self.events.for_region(self.region) as connection:
            event = self.events.lock_event(connection, self.region, event_id)
            attempt = self._get_attempt(connection, event_id, attempt_id, worker_id)
            if attempt["status"] == "failed":
                return attempt  # A duplicate failure must not change a newer claim.

            self._check_owner(event, attempt)
            failed = connection.execute("""
                UPDATE review.attempts
                SET status = 'failed', finished_at = clock_timestamp(), error = %s
                WHERE id = %s AND region_id = %s AND status = 'running'
                  AND expires_at > clock_timestamp()
                RETURNING *
            """, (error, attempt_id, self.region)).fetchone()
            if failed is None:
                raise ClaimConflict("The claim expired; this failure is stale.")
            status = "failed" if event["attempts_used"] >= MAX_ATTEMPTS else "available"
            connection.execute("""
                UPDATE review.events SET status = %s, active_attempt_id = NULL
                WHERE id = %s AND region_id = %s
            """, (status, event_id, self.region))
            return failed

    def retry_failed(self, event_id: UUID) -> None:
        # The supervisor endpoint supplies the account's region.
        with self.events.for_region(self.region) as connection:
            event = self.events.lock_event(connection, self.region, event_id)
            if event["status"] != "failed":
                raise ClaimConflict("Only a failed event can be retried. Refresh its current status.")
            connection.execute("""
                UPDATE review.events SET status = 'available', attempts_used = 0
                WHERE id = %s AND region_id = %s
            """, (event_id, self.region))

    def _get_attempt(self, connection, event_id: UUID, attempt_id: UUID, worker_id: str) -> dict:
        # Read after locking the event: another worker may have finished while we waited.
        attempt = connection.execute("""
            SELECT * FROM review.attempts
            WHERE id = %s AND event_id = %s AND region_id = %s AND worker_id = %s
        """, (attempt_id, event_id, self.region, worker_id)).fetchone()
        if attempt is None:
            raise ClaimConflict("Attempt does not belong to this event and worker.")
        return attempt

    def _check_owner(self, event: dict, attempt: dict) -> None:
        if (event["status"] != "claimed" or event["active_attempt_id"] != attempt["id"]
                or attempt["status"] != "running"):
            raise ClaimConflict("This attempt no longer owns the event.")
