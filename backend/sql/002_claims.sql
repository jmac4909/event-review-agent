-- Attempt history and accepted results are separate from the event itself.
ALTER TABLE review.events
    ADD COLUMN attempts_used integer NOT NULL DEFAULT 0 CHECK (attempts_used >= 0),
    ADD COLUMN active_attempt_id uuid,
    ADD CONSTRAINT events_id_region UNIQUE (id, region_id);

CREATE TABLE review.attempts (
    id uuid PRIMARY KEY,
    event_id uuid NOT NULL,
    region_id text NOT NULL,
    worker_id text NOT NULL CHECK (length(worker_id) BETWEEN 1 AND 100),
    started_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > started_at),
    status text NOT NULL CHECK (status IN ('running', 'completed', 'expired', 'failed')),
    finished_at timestamptz,
    error text,
    FOREIGN KEY (event_id, region_id) REFERENCES review.events (id, region_id),
    UNIQUE (id, event_id, region_id),
    CHECK ((status = 'running') = (finished_at IS NULL))
);

-- An expired attempt is closed before its replacement is inserted.
CREATE UNIQUE INDEX attempts_one_running ON review.attempts (event_id)
    WHERE status = 'running';
CREATE INDEX attempts_event_history ON review.attempts (region_id, event_id, started_at);

ALTER TABLE review.events
    ADD CONSTRAINT events_active_attempt FOREIGN KEY (active_attempt_id, id, region_id)
        REFERENCES review.attempts (id, event_id, region_id),
    ADD CONSTRAINT events_claim_has_attempt CHECK (
        (status = 'claimed') = (active_attempt_id IS NOT NULL)
    );

CREATE TABLE review.reviews (
    event_id uuid PRIMARY KEY,
    region_id text NOT NULL,
    attempt_id uuid NOT NULL UNIQUE,
    result jsonb NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (attempt_id, event_id, region_id)
        REFERENCES review.attempts (id, event_id, region_id)
);

ALTER TABLE review.attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE review.attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY attempts_region ON review.attempts
    USING (region_id = nullif(current_setting('app.region', true), ''))
    WITH CHECK (region_id = nullif(current_setting('app.region', true), ''));

ALTER TABLE review.reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE review.reviews FORCE ROW LEVEL SECURITY;
CREATE POLICY reviews_region ON review.reviews
    USING (region_id = nullif(current_setting('app.region', true), ''))
    WITH CHECK (region_id = nullif(current_setting('app.region', true), ''));

GRANT UPDATE (status, attempts_used, active_attempt_id) ON review.events TO review_app;
GRANT SELECT, INSERT ON review.attempts, review.reviews TO review_app;
GRANT UPDATE (status, finished_at, error) ON review.attempts TO review_app;
-- Accepted results, worker identity, event metadata and deadlines cannot be edited.
