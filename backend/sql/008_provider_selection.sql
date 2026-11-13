-- Null preserves the environment default and all existing source controls.
ALTER TABLE review.event_feed ADD COLUMN selected_reviewer text
    CHECK (selected_reviewer IN ('mock', 'anthropic', 'codex'));
GRANT UPDATE (selected_reviewer) ON review.event_feed TO review_app;

-- Capture the selection when claiming, before any model call. Historical
-- attempts keep their existing model_name/trace without invented provenance.
ALTER TABLE review.attempts ADD COLUMN reviewer_mode text
    CHECK (reviewer_mode IN ('mock', 'anthropic', 'codex'));
-- The existing INSERT grant covers this column; runtime UPDATE does not.
