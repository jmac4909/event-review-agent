-- Additive: earlier workers and saved reviews remain readable.
ALTER TABLE review.attempts ADD COLUMN steps jsonb NOT NULL DEFAULT '[]'
    CHECK (jsonb_typeof(steps) = 'array' AND jsonb_array_length(steps) <= 7);
GRANT UPDATE (steps) ON review.attempts TO review_app;

CREATE TABLE review.site_context (
    region_id text NOT NULL CHECK (region_id IN ('europe', 'us', 'asia')),
    site text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    valid_from timestamptz NOT NULL,
    source_ref text NOT NULL,
    content jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'),
    PRIMARY KEY (region_id, site, version)
);
ALTER TABLE review.site_context ENABLE ROW LEVEL SECURITY;
ALTER TABLE review.site_context FORCE ROW LEVEL SECURITY;
CREATE POLICY site_context_region ON review.site_context
    USING (region_id = nullif(current_setting('app.region', true), ''))
    WITH CHECK (region_id = nullif(current_setting('app.region', true), ''));
REVOKE ALL ON review.site_context FROM PUBLIC;
GRANT SELECT ON review.site_context TO review_app;
