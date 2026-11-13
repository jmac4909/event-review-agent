CREATE TABLE review.event_feed (
    region_id text PRIMARY KEY CHECK (region_id IN ('europe', 'us', 'asia')),
    enabled boolean NOT NULL DEFAULT false,
    model_mode text CHECK (model_mode IN ('mock', 'anthropic', 'codex')),
    version integer NOT NULL DEFAULT 0 CHECK (version >= 0),
    next_event_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    generated bigint NOT NULL DEFAULT 0 CHECK (generated >= 0),
    last_event_id uuid,
    last_event_at timestamptz,
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (NOT enabled OR model_mode IS NOT NULL),
    CHECK ((last_event_id IS NULL) = (last_event_at IS NULL)),
    FOREIGN KEY (last_event_id, region_id) REFERENCES review.events (id, region_id)
);

-- Create only these initial controls. Later setup runs preserve human choices.
INSERT INTO review.event_feed (region_id) VALUES ('europe'), ('us'), ('asia');

ALTER TABLE review.event_feed ENABLE ROW LEVEL SECURITY;
ALTER TABLE review.event_feed FORCE ROW LEVEL SECURITY;
CREATE POLICY event_feed_region ON review.event_feed
    USING (region_id = nullif(current_setting('app.region', true), ''))
    WITH CHECK (region_id = nullif(current_setting('app.region', true), ''));
REVOKE ALL ON review.event_feed FROM PUBLIC;
GRANT SELECT ON review.event_feed TO review_app;
GRANT UPDATE (enabled, model_mode, version, next_event_at, generated,
              last_event_id, last_event_at, updated_by, updated_at)
    ON review.event_feed TO review_app;
