"""Small, explicit SQL access layer with a transaction-scoped region."""

from collections.abc import Generator
from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from .settings import Settings


REGIONS = frozenset({"europe", "us", "asia"})
EVENT_COLUMNS = """
    id, region_id, source_ref, event_type, title, description,
    site, zone, camera, occurred_at, status, metadata, simulation_scenario
"""


class UnsafeDatabaseRole(RuntimeError):
    pass


class EventNotFound(LookupError):
    pass


class EventStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def connection(self) -> Generator[psycopg.Connection, None, None]:
        with psycopg.connect(
            host=self.settings.pg_host,
            port=self.settings.pg_port,
            dbname=self.settings.pg_database,
            user="review_app",
            connect_timeout=3,
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            # RLS is ineffective if the application accidentally uses an owner
            # or privileged role. Check the actual runtime connection.
            role = connection.execute("""
                SELECT rolsuper, rolbypassrls,
                       pg_has_role(current_user, 'review_owner', 'MEMBER') AS is_owner
                FROM pg_roles WHERE rolname = current_user
            """).fetchone()
            if role["rolsuper"] or role["rolbypassrls"] or role["is_owner"]:
                raise UnsafeDatabaseRole("The runtime role must not own or bypass regional data.")
            yield connection

    @contextmanager
    def for_region(self, region: str) -> Generator[psycopg.Connection, None, None]:
        if region not in REGIONS:
            raise ValueError("A known region is required.")
        with self.connection() as connection:
            with connection.transaction():
                connection.execute("SET LOCAL statement_timeout = '3s'")
                connection.execute("SELECT set_config('app.region', %s, true)", (region,))
                yield connection

    def list_events(
        self, region: str, status: str | None = None, *,
        category: str | None = None, severity: str | None = None,
        event_type: str | None = None, query: str = "",
        before_at=None, before_id: UUID | None = None,
    ) -> dict:
        with self.for_region(region) as connection:
            page = connection.execute(
                f"""
                WITH regional AS (
                    SELECT {EVENT_COLUMNS} FROM review.events WHERE region_id = %(region)s
                ), assessed AS (
                    SELECT e.*, CASE WHEN r.event_id IS NULL THEN NULL ELSE
                        jsonb_build_object(
                            'decision', coalesce(c.decision, r.result->>'decision'),
                            'severity', CASE WHEN c.decision IS NOT NULL THEN c.severity ELSE r.result->>'severity' END,
                            'source', CASE WHEN c.decision IS NOT NULL THEN 'supervisor'
                                           WHEN a.model_output->>'source' = 'harness' THEN 'harness' ELSE 'agent' END,
                            'model_name', a.model_name
                        ) END AS assessment
                    FROM regional e
                    LEFT JOIN review.reviews r ON r.event_id = e.id AND r.region_id = e.region_id
                    LEFT JOIN review.attempts a ON a.id = r.attempt_id AND a.region_id = e.region_id
                    LEFT JOIN LATERAL (
                        SELECT decision, severity FROM review.corrections
                        WHERE event_id = e.id AND region_id = e.region_id
                        ORDER BY version DESC LIMIT 1
                    ) c ON true
                ), filtered AS (
                    SELECT * FROM assessed
                    WHERE (%(status)s::text IS NULL OR status = %(status)s)
                      AND (%(category)s::text IS NULL OR CASE %(category)s
                          WHEN 'safety_events' THEN assessment->>'decision' = 'confirmed'
                          WHEN 'dismissed' THEN assessment->>'decision' = 'rejected'
                          WHEN 'needs_input' THEN status = 'needs_input'
                          WHEN 'pending' THEN status IN ('available', 'claimed')
                          WHEN 'failed' THEN status = 'failed'
                          ELSE false END)
                      AND (%(severity)s::text IS NULL OR assessment->>'severity' = %(severity)s)
                      AND (%(event_type)s::text IS NULL OR event_type = %(event_type)s)
                      AND strpos(lower(concat_ws(' ', title, site, zone, camera, source_ref)), lower(%(query)s)) > 0
                ), selected AS (
                    SELECT * FROM filtered
                    WHERE (%(before_at)s::timestamptz IS NULL OR occurred_at < %(before_at)s
                           OR (occurred_at = %(before_at)s AND id > %(before_id)s))
                    ORDER BY occurred_at DESC, id LIMIT 26
                )
                SELECT coalesce((SELECT jsonb_agg(e ORDER BY e.occurred_at DESC, e.id) FROM selected e), '[]') AS items,
                    (SELECT count(*) FROM filtered) AS matched,
                    ARRAY(SELECT DISTINCT event_type FROM regional ORDER BY event_type) AS event_types,
                    jsonb_build_object(
                        'total', count(*),
                        'available', count(*) FILTER (WHERE status = 'available'),
                        'claimed', count(*) FILTER (WHERE status = 'claimed'),
                        'reviewed', count(*) FILTER (WHERE status = 'reviewed'),
                        'needs_input', count(*) FILTER (WHERE status = 'needs_input'),
                        'failed', count(*) FILTER (WHERE status = 'failed'),
                        'safety_events', count(*) FILTER (WHERE assessment->>'decision' = 'confirmed')
                    ) AS counts
                FROM assessed
                """, {"region": region, "status": status, "category": category,
                        "severity": severity, "event_type": event_type, "query": query.strip(),
                        "before_at": before_at, "before_id": before_id},
            ).fetchone()
        # The extra row tells us whether another page exists. A time/id cursor
        # keeps new arrivals from shifting the older events between pages.
        page["next_page"] = None
        if len(page["items"]) > 25:
            page["items"] = page["items"][:25]
            last = page["items"][-1]
            page["next_page"] = {"occurred_at": last["occurred_at"], "id": last["id"]}
        return page

    def get_event(self, region: str, event_id: UUID) -> dict | None:
        with self.for_region(region) as connection:
            return connection.execute(
                f"SELECT {EVENT_COLUMNS} FROM review.events WHERE region_id = %s AND id = %s",
                (region, event_id),
            ).fetchone()

    def lock_event(self, connection, region: str, event_id: UUID) -> dict:
        """Lock within the caller's regional transaction so later writes stay atomic."""
        event = connection.execute("""
            SELECT * FROM review.events WHERE id = %s AND region_id = %s FOR UPDATE
        """, (event_id, region)).fetchone()
        if event is None:
            raise EventNotFound("Event not found.")
        return event

    def check_connection(self) -> None:
        with self.connection() as connection:
            connection.execute("SELECT 1")

    def inspect_event(self, region: str, event_id: UUID) -> dict | None:
        with self.for_region(region) as connection:
            # One SQL statement gives event, history, and result the same snapshot.
            return connection.execute("""
                SELECT e.*,
                    COALESCE((SELECT jsonb_agg(a ORDER BY a.started_at DESC, a.id)
                              FROM review.attempts a
                              WHERE a.event_id = e.id AND a.region_id = e.region_id), '[]') AS attempts,
                    (SELECT to_jsonb(r) FROM review.reviews r
                     WHERE r.event_id = e.id AND r.region_id = e.region_id) AS review,
                    COALESCE((SELECT jsonb_agg(c ORDER BY c.version DESC)
                              FROM review.corrections c
                              WHERE c.event_id = e.id AND c.region_id = e.region_id), '[]') AS corrections,
                    (SELECT jsonb_build_object('id', g.id, 'status', g.status,
                                               'source_correction_id', g.source_correction_id)
                     FROM review.guidance g
                     JOIN review.corrections c ON c.id = g.source_correction_id AND c.region_id = g.region_id
                     WHERE g.source_event_id = e.id AND g.region_id = e.region_id
                     ORDER BY c.version DESC LIMIT 1) AS guidance
                FROM review.events e WHERE e.id = %s AND e.region_id = %s
            """, (event_id, region)).fetchone()

