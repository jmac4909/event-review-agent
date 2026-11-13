-- Run as the migration owner, never as the application role.
CREATE SCHEMA IF NOT EXISTS review AUTHORIZATION review_owner;
REVOKE ALL ON SCHEMA review FROM PUBLIC;
GRANT USAGE ON SCHEMA review TO review_app;

CREATE TABLE review.events (
    id uuid PRIMARY KEY,
    region_id text NOT NULL CHECK (region_id IN ('europe', 'us', 'asia')),
    source_ref text NOT NULL,
    event_type text NOT NULL,
    title text NOT NULL,
    description text NOT NULL,
    site text NOT NULL,
    zone text NOT NULL,
    camera text NOT NULL,
    occurred_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'available' CHECK (
        status IN ('available', 'claimed', 'reviewed', 'needs_input', 'failed')
    ),
    metadata jsonb NOT NULL CHECK (jsonb_typeof(metadata) = 'object'),
    ingested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (region_id, source_ref)
);
CREATE INDEX events_region_time ON review.events (region_id, occurred_at DESC, id);

ALTER TABLE review.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE review.events FORCE ROW LEVEL SECURITY;
CREATE POLICY events_region ON review.events
    USING (region_id = nullif(current_setting('app.region', true), ''))
    WITH CHECK (region_id = nullif(current_setting('app.region', true), ''));

-- Slice 1 is read-only. Later migrations grant only the new operations needed.
REVOKE ALL ON review.events FROM PUBLIC;
GRANT SELECT ON review.events TO review_app;

