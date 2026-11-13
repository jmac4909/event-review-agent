# AI assistance

I built this project with substantial assistance from Codex. It helped me compare approaches, draft application code, SQL, prompts, frontend components, tests, development scripts and documentation, and investigate failures. Much of the implementation was AI-drafted and refined through our review.

I made the key design decisions: Python/FastAPI, PostgreSQL with regional row-level security, fixed work claims, three automatic attempts, local hybrid memory retrieval, and separate supervisor approval for reusable guidance. I chose to keep the harness explicit: Python controls claims, context, permitted lookups, validation and persistence; the model interprets the supplied evidence and returns a verdict or a lookup request. I also decided that approved guidance should be considered despite changed measurements, with the model checking its written conditions against each new event.

I reviewed the implementation and worked through the harness, database transactions, claim expiry and stale-worker protection, region isolation, retries, and the feedback-to-memory flow. I exercised the UI, corrected alerts, approved guidance, and checked how later events used it. That review drove changes to the approval workflow, matching rules, dated examples, model selection and code readability.

The assistant helped implement and run the automated checks and inspect browser behavior. The [verification record](verification.md) distinguishes offline tests, live-provider evaluations and deployment checks. Test fixtures that simulate supervisor actions are identified as such.

The application harness is custom Python; no agent framework implements its loop. Anthropic uses the official SDK directly. The optional Codex practice adapter invokes the Codex CLI, which has its own internal harness, while returning results through the same application validation and claim checks. No API keys, personal login credentials or confidential assignment materials are included in the repository.
