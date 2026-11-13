ALTER TABLE review.event_feed
    ADD COLUMN run_seconds integer DEFAULT 300 CHECK (run_seconds IN (300, 600)),
    ADD COLUMN stops_at timestamptz;
GRANT UPDATE (run_seconds, stops_at) ON review.event_feed TO review_app;

-- Give streams already running at upgrade five more minutes. Keep their
-- selected model, cursor, events and reviews. Paused streams stay paused.
DO $$
DECLARE target_region text;
BEGIN
    FOREACH target_region IN ARRAY ARRAY['europe', 'us', 'asia'] LOOP
        PERFORM set_config('app.region', target_region, true);
        UPDATE review.event_feed
        SET stops_at = clock_timestamp() + interval '5 minutes', version = version + 1,
            updated_by = 'auto-stop-setup', updated_at = clock_timestamp()
        WHERE region_id = target_region AND enabled;
    END LOOP;
    PERFORM set_config('app.region', '', true);
END $$;
