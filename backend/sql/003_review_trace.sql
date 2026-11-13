-- Keep the input even when a model call or verdict validation fails.
ALTER TABLE review.attempts
    ADD COLUMN context jsonb,
    ADD COLUMN model_name text,
    ADD COLUMN model_started_at timestamptz,
    ADD COLUMN model_finished_at timestamptz,
    ADD COLUMN model_output jsonb;

GRANT UPDATE (context, model_name, model_started_at, model_finished_at, model_output)
    ON review.attempts TO review_app;
