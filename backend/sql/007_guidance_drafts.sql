-- Existing suggestions keep their source wording; only new corrections queue a draft.
ALTER TABLE review.guidance
    ADD COLUMN draft_status text NOT NULL DEFAULT 'not_requested'
        CHECK (draft_status IN ('not_requested', 'queued', 'running', 'completed', 'failed', 'cancelled')),
    ADD COLUMN draft_result jsonb CHECK (jsonb_typeof(draft_result) = 'object'),
    ADD COLUMN draft_history jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(draft_history) = 'array'),
    ADD COLUMN draft_attempt_id uuid,
    ADD COLUMN draft_worker_id text,
    ADD COLUMN draft_expires_at timestamptz,
    ADD COLUMN draft_attempts_used integer NOT NULL DEFAULT 0 CHECK (draft_attempts_used BETWEEN 0 AND 3);
CREATE INDEX guidance_draft_queue ON review.guidance (region_id, created_at)
    WHERE status = 'proposed' AND draft_status IN ('queued', 'running');
GRANT UPDATE (draft_status, draft_result, draft_history, draft_attempt_id,
              draft_worker_id, draft_expires_at, draft_attempts_used)
    ON review.guidance TO review_app;
