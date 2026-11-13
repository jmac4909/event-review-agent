ALTER TABLE review.reviews ADD CONSTRAINT reviews_event_region UNIQUE (event_id, region_id);

CREATE TABLE review.corrections (
    id uuid PRIMARY KEY,
    event_id uuid NOT NULL,
    region_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    decision text NOT NULL CHECK (decision IN ('confirmed', 'rejected')),
    severity text CHECK (severity IN ('low', 'medium', 'high')),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 2000),
    supervisor_id text NOT NULL,
    event_snapshot jsonb NOT NULL CHECK (jsonb_typeof(event_snapshot) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((decision = 'confirmed') = (severity IS NOT NULL)),
    UNIQUE (event_id, version),
    UNIQUE (id, event_id, region_id),
    FOREIGN KEY (event_id, region_id) REFERENCES review.reviews (event_id, region_id)
);

CREATE TABLE review.guidance (
    id uuid PRIMARY KEY,
    region_id text NOT NULL,
    source_event_id uuid NOT NULL,
    source_correction_id uuid NOT NULL UNIQUE,
    event_type text NOT NULL,
    site text NOT NULL,
    zone text NOT NULL,
    camera text NOT NULL,
    metadata_condition jsonb NOT NULL CHECK (jsonb_typeof(metadata_condition) = 'object'),
    proposed_text text NOT NULL,
    approved_text text,
    decision text NOT NULL CHECK (decision IN ('confirmed', 'rejected')),
    severity text CHECK (severity IN ('low', 'medium', 'high')),
    status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'approved', 'rejected', 'retired', 'superseded')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    decided_at timestamptz,
    decided_by text,
    retired_at timestamptz,
    retired_by text,
    CHECK ((decision = 'confirmed') = (severity IS NOT NULL)),
    CHECK (status <> 'approved' OR (approved_text IS NOT NULL AND decided_at IS NOT NULL AND decided_by IS NOT NULL)),
    FOREIGN KEY (source_correction_id, source_event_id, region_id)
        REFERENCES review.corrections (id, event_id, region_id)
);
CREATE INDEX corrections_region_recent ON review.corrections (region_id, created_at DESC);
CREATE INDEX guidance_scope ON review.guidance (region_id, event_type, site, zone, camera, status);

ALTER TABLE review.corrections ENABLE ROW LEVEL SECURITY;
ALTER TABLE review.corrections FORCE ROW LEVEL SECURITY;
CREATE POLICY corrections_region ON review.corrections
    USING (region_id = nullif(current_setting('app.region', true), ''))
    WITH CHECK (region_id = nullif(current_setting('app.region', true), ''));
ALTER TABLE review.guidance ENABLE ROW LEVEL SECURITY;
ALTER TABLE review.guidance FORCE ROW LEVEL SECURITY;
CREATE POLICY guidance_region ON review.guidance
    USING (region_id = nullif(current_setting('app.region', true), ''))
    WITH CHECK (region_id = nullif(current_setting('app.region', true), ''));
GRANT SELECT, INSERT ON review.corrections, review.guidance TO review_app;
GRANT UPDATE (approved_text, decision, severity, status, decided_at, decided_by, retired_at, retired_by)
    ON review.guidance TO review_app;

-- Deliberate mock ingestion creates a new event; it never resets a reviewed one.
ALTER TABLE review.events ADD COLUMN simulated_from_event_id uuid,
    ADD CONSTRAINT events_simulation_parent FOREIGN KEY (simulated_from_event_id, region_id)
        REFERENCES review.events (id, region_id);
GRANT INSERT (id, region_id, source_ref, event_type, title, description, site, zone,
              camera, occurred_at, metadata, simulated_from_event_id) ON review.events TO review_app;
