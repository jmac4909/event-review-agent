# Verification

Checks below use synthetic data. Automated supervisor actions are evaluation fixtures; the UI supports the same correction and approval flow.

| Area | Completed check |
| --- | --- |
| Backend | 300 tests passed in 152.18 seconds against real PostgreSQL in the packaged Linux ARM image on November 13, 2026. Ran `docker compose --profile test run --build --rm test` in an isolated Compose project with fresh test volumes. External model sockets were blocked. |
| Claims and recovery | Concurrent claims, fixed expiry and reassignment, late-worker rejection, three-attempt limits, duplicate submissions and uncertain save acknowledgements. Tests advance stored deadlines instead of waiting fifteen minutes. |
| Region isolation | Forced RLS and restricted database roles, cross-region IDs, memory selection and stale browser sessions. |
| Feedback and memory | Correction, draft, human approval, later review, conflicts, superseded guidance and stopped-rule exclusion. Stopping a rule also excludes its source correction from future memory. |
| Docker | Fresh volumes on Linux ARM, with cached build dependencies. All ten acceptance checks passed on November 13, 2026: keyless startup, feedback, global Mock mode, isolation and restart persistence across all seven regional tables. |
| Global model switch | Mock overrides saved regional choices for reviews and drafts in all regions. Provider-routing tests cover the corresponding real modes. |
| Browser | Chromium at desktop and mobile viewport sizes: 1440×1000, 390×844 and 844×390. Journeys cover corrections, approvals, later reviews, filtering, pagination, stale sessions and failed requests. Screenshots were inspected; the stopped-guidance journey passed again on November 13. |
| Hosted runtime | All 25 health, configuration and data-preservation checks passed. Hosted HTTPS checks verified regional sign-in and provider controls. A live Claude call from the deployed worker also passed in a disposable database. |
| Hosted access | Missing and incorrect access codes and sessions created before the gate was enabled are rejected. Sign-in, reload, sign-out and cross-region isolation passed for all three regions. Login screens were checked in Chromium at desktop and mobile viewport sizes. |
| Live Anthropic | The supplied key accessed `claude-sonnet-5-5`. The direct SDK path passed the severity feedback loop and ten follow-up cases covering distance, duration, count, dates and conflicts. Two real initial reviews and three generated drafts also passed, including rejection of a case-specific permission as reusable policy. |
| Live Codex | Date exceptions, changed measurements, conflicting rules and case-specific permission were exercised through the application harness. |

## Live model evaluation

The initial 4.99 m test exposed a verdict that contradicted its explanation. I added a prompt instruction to check the verdict against the rule boundary. The revised prompt (`review-context-v11`) returned the expected verdicts on all ten cases.

The affected context, worker and provider tests also passed: **56 tests**.

The [recorded Claude feedback loop](anthropic-feedback.json) contains the exact before/after inputs, outputs, draft and approval. The original review remains medium severity; the later review follows the approved high-severity policy.

## Repeat the checks

Use the commands in [README](../README.md#verify). `scripts/check_compose.py` creates and removes its own stack. The backend/browser helpers and live-provider scripts use disposable databases and retain their reports under ignored `.work/`.

`scripts/check_model.py --mode anthropic` exercises an initial review, supervisor correction, generated suggestion, scripted approval and a changed later verdict. `scripts/check_guidance.py --mode anthropic --output .work/guidance-check.json` evaluates dates, distance/duration/count conditions and conflicting rules. These commands make paid calls and require the key in the environment. Routine tests use deterministic Mock or fake transports.
