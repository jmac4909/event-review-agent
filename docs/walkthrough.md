# Code and demo walkthrough

Start with one event and follow its data. The review loop, tool dispatch, transactions, and approval decisions are ordinary Python.

The tables below identify the main code paths; the examples show how to exercise them.

## Read the code in this order

| File | What to follow |
| --- | --- |
| [`app.py`](../backend/src/event_review/app.py) | The HTTP routes and the region taken from the signed session |
| [`models.py`](../backend/src/event_review/models.py) | Request/response DTOs and valid verdict combinations |
| [`database.py`](../backend/src/event_review/database.py) | Restricted connection, regional transaction, event queries |
| [`worker.py`](../backend/src/event_review/worker.py) | `run_once()` owns an event review; `investigate()` decides whether to finish or look up more evidence; `_call_model()` and `_run_lookup()` record each step |
| [`claims.py`](../backend/src/event_review/claims.py) | Ownership, expiration, retry budget, and conditional saves |
| [`reviewer.py`](../backend/src/event_review/reviewer.py), [`providers.py`](../backend/src/event_review/providers.py) | Context instructions, deterministic Mock, direct Anthropic and optional Codex calls |
| [`investigation.py`](../backend/src/event_review/investigation.py) | The two allowed lookups and evidence limits |
| [`memory.py`](../backend/src/event_review/memory.py), [`retrieval.py`](../backend/src/event_review/retrieval.py) | Corrections, human approval, eligibility, then ranking |
| [`drafts.py`](../backend/src/event_review/drafts.py), [`feed.py`](../backend/src/event_review/feed.py) | Background suggestions and recurring synthetic arrivals |
| [`Workspace.jsx`](../frontend/src/Workspace.jsx) | Fetching, filters, selected event, and panel navigation |
| [`SupervisorForm.jsx`](../frontend/src/SupervisorForm.jsx), [`GuidanceCard.jsx`](../frontend/src/GuidanceCard.jsx) | The two distinct human actions: fix this event; approve a reusable lesson |
| [`MemoryPanel.jsx`](../frontend/src/MemoryPanel.jsx), [`AttemptHistory.jsx`](../frontend/src/AttemptHistory.jsx) | The guidance list and the recorded review steps |
| [`simulation.py`](../backend/src/event_review/simulation.py), [`SimulationForm.jsx`](../frontend/src/SimulationForm.jsx) | Named date/distance inputs; the ordinary worker decides their outcome |

The SQL migrations define the tables used by the store methods. `embeddings.py`, `settings.py`, `auth.py`, and `context.py` support these paths. `scripts/dev.py` creates disposable local test environments.

## Follow one review

1. `FeedStore.ingest_due()` inserts an event when its regional stream is due.
2. `ClaimStore.claim_next()` locks an eligible row and creates an attempt UUID with a deadline.
3. `MemoryStore.select_for()` applies scope/trust checks, then ranks eligible evidence. `Investigation.site_rules()` obtains the baseline rules valid at occurrence time.
4. `build_context()` combines those inputs. `model_context()` removes retrieval diagnostics from the payload sent to the adapter, keeping the audit copy intact.
5. `Worker.investigate()` calls `_call_model()`, which records the input, response and usage. A valid lookup response goes to `_run_lookup()` and then back around the loop. A valid verdict ends the loop.
6. The claim store verifies ownership and deadline again before accepting the result. The UI polls the event detail and displays it.

The context sample in [`example-context.json`](example-context.json) shows an actual Mock run before and after approval. Open `before.attempts[0].steps[0].input` and `after.attempts[0].steps[0].input` for the exact application inputs. Their adjacent `output` fields contain the replies. These are recorded snapshots, not reconstructions from today's database.

The worker handles provider failures separately from database failures. A provider failure can use a retry; a lost database acknowledgement may mean the save already succeeded. That is why saving the final review sits outside the model-error handler. Stale claims are discarded, and uncertain database writes are left for reconciliation or expiry.

## Follow a correction

1. `SupervisorForm` sends an outcome, reason, request ID and expected correction version.
2. `MemoryStore.correct()` saves the correction and queues a guidance suggestion. The original model review stays intact.
3. `Worker.draft_once()` asks the model for reusable wording. `DraftStore` records that attempt; it cannot approve the rule.
4. `GuidanceCard` lets the supervisor edit and approve the wording. `MemoryStore.approve()` records that human decision.
5. A later review's `select_for()` retrieves approved guidance within the same scope. The model checks its written conditions against the new event.

Three identifiers protect different things: the **attempt ID** identifies the worker claim, the **request ID** prevents a duplicate save after a lost response, and the **expected version** prevents one supervisor from overwriting another's update.

## Main demo: a one-day exception

Use Claude or the optional Codex adapter for this example. Open an **Access alert** in the event feed. If matching dated guidance is already approved on the hosted preview, inspect it and skip to step 4. For a fresh demonstration, use a disposable database so prior guidance does not determine the initial result.

1. Open **Simulate similar event**, select **Same evidence · current time**, and choose **Create event**. The baseline asks whether entry was authorized.
2. Dismiss that alert with: “Entry to this zone is unrestricted on [the event's date] only. Other dates still require authorization.” Replace the bracketed date with the UTC date shown on the event.
3. Review the suggested wording and approve it. Check that it preserves the date and scope.
4. From that source event, create a **Same day** event. The approved exception should dismiss it.
5. From the source event again, create a **Two days later** event. It should ask for authorization because the exception does not cover that date. A future occurrence is visibly labelled **Future-date test**.
6. Inspect each review's saved context and explanation. The rule can be retrieved for both dates; the model decides whether its condition applies. The relevant date is the event's occurrence time, not today's date or the time of review.

The dates follow the selected event, so the example works on any recording day. The simulator does not approve guidance or choose a verdict.

## Second demo: require 5 m separation

Open a **Vehicle proximity** event in the feed. Check existing guidance first; use a fresh database for an uncontaminated baseline.

1. Create a **3 m separation** event. The baseline 2 m rule dismisses it.
2. Correct it to a confirmed safety event, with **High** severity for this synthetic example. Explain: “This camera requires at least 5 metres separation. Confirm separations below 5 metres with high severity; dismiss at 5 metres or more.”
3. Review and approve the suggested wording. The model should preserve the threshold rather than limiting reuse to exactly 3 m.
4. Create **3 m**, **4.99 m**, and **5 m** events. Expect confirmed/high, confirmed/high, and dismissed respectively.
5. Inspect the saved guidance and changed measurement in each context. Confirm the original review remains unchanged.

The presets change the selected measurement while retaining the other source fields. A repeated Create request returns the same event; changing the scenario under that request ID is rejected.

`scripts/check_guidance.py` exercises both stories with the real provider in a disposable database, including initial reviews, generated drafts and scripted human approvals. It also checks that one person's permission produces no reusable rule. Its remaining cases cover durations, counts and conflicting guidance.

## Demo: learn a severity policy

Use a fresh local database with the default global **Mock** mode for this repeatable example. On the hosted demo, check existing guidance before comparing the baseline.

1. Sign in as **Europe supervisor**. Search for `europe-fixture-3` and open the blocked-exit event at North warehouse / East exit. The supplied evidence is a pallet observed for 95 seconds. The baseline rule confirms blocking at 60 seconds or more, with medium severity.
2. Open the original review and its attempt history. Show the event metadata, upfront site rule, exact model input, and confirmed/medium result.
3. Choose **Correct this review**. Keep the alert confirmed, change severity to **High**, and enter:

   > Site policy: Matching 95-second pallet obstructions at East exit are high severity. This is a standing severity rule for future matching events.

4. Save. The event now shows the human outcome while retaining the original review. Open **Memory** and the proposed guidance. The background draft suggests wording; it is still untrusted.
5. Read the rule and source correction, edit wording if needed, then choose **Approve guidance**. Severity is inherited from the correction; there is no second severity choice.
6. Return to the source event, open **Simulate similar event**, keep **Same evidence · current time**, and choose **Create event**. The new review should be confirmed/high, citing the approved guidance. Show that guidance in the saved model input and compare the unchanged original review.
7. Switch to another region. The source event and Europe memory are unavailable there.

For a second scenario, open the access-zone event. Unknown authorization produces **Needs input**. The supervisor resolves that case by confirming or dismissing it with a reason. One person's permission should not become permission for everybody; approve only a clearly reusable rule with an appropriate scope. Real models can interpret ranges, dates and conditional exceptions. Mock supports only the explicit standing-severity example; other approved wording produces Needs input.

## Explain the failure paths

| Situation | Expected behavior |
| --- | --- |
| Two workers race | One claims the row; the other skips the lock |
| Worker stalls past its deadline | Another can claim; the old attempt cannot save |
| Provider times out or returns invalid output | Attempt recorded; up to three automatic attempts |
| All attempts fail | Visible failed state and explicit retry action |
| Save succeeds but its response is lost | Same request ID/read-back finds the existing result |
| Two supervisors edit the same event | Expected-version check rejects the stale edit |
| Correction changes while a draft runs | Old guidance is superseded; late draft cannot become approved |
| Applicable approved rules conflict | The model asks for input after evaluating their conditions |
| Too much guidance for one review | The harness requires input before making a model call |
| A new region signs in | Old workspace drafts and selections are cleared |

## Run the examples

The [AI disclosure](ai-assistance.md) describes the collaboration, and the [verification record](verification.md) lists completed checks.

To run the date, distance, duration, count and conflict cases with Claude, export `ANTHROPIC_API_KEY`, set the PostgreSQL socket settings for your environment, then run:

```sh
.venv/bin/python scripts/check_guidance.py --mode anthropic --output .work/guidance-check.json
```

This uses a disposable database and scripted supervisor approvals, makes real model calls, and saves the inputs and results. `--mode codex` runs the same evaluation through the optional Codex adapter.
