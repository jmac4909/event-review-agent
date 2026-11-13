# Event Review

A full-stack prototype for reviewing regional safety alerts and learning from supervisor feedback. It uses Python/FastAPI, PostgreSQL, React, and an explicit Python worker loop. No agent framework is used.

The agent reviews synthetic detector metadata. A supervisor can answer a question or correct a verdict, then separately approve guidance for future matching events. Every accepted review keeps its original context, model calls, lookups, result, and timing.

Hosted demo: **https://review.jmac.page**. Enter the reviewer access code, then choose a regional supervisor and inspect an alert. Each review records the provider that produced it. Mock runs offline; Claude uses the official Anthropic SDK; Codex is an optional practice adapter.

[Watch the recorded walkthrough](https://www.loom.com/share/253ef7d8a36a49eba6e7cbc559d3cd96). The Loom uses Codex; the [recorded Claude feedback loop](docs/anthropic-feedback.json) shows the same flow through Claude's SDK.

Start with the [demo walkthrough](docs/walkthrough.md). See [architecture and trade-offs](docs/architecture.md), [verification results](docs/verification.md), and [AI assistance](docs/ai-assistance.md).

## Run

### Mock (default)

```sh
docker compose up --build
```

Open http://localhost:8116. No API key or `.env` is needed. API documentation is at http://localhost:8116/api/docs. Compose starts PostgreSQL, migration/seed setup, the API, and six worker threads: two for each region. There are twelve starting events. Setup preserves existing records.

The first build needs network access for dependencies and a pinned, checksum-verified local embedding model (about 31 MB). Mock reviews run without network afterward. Data persists in named volumes. To restart while retaining it:

```sh
docker compose down
docker compose up --detach --wait
```

Keep the volumes to preserve your data. The database uses a private Unix socket, and sign-in selects a regional demo account.

Optional settings are in [.env.example](.env.example). Copy it to an untracked `.env` for Compose, or export variables for the local Python commands below.

| Setting | Default / purpose |
| --- | --- |
| `REVIEW_MODEL_MODE` | `mock`, `anthropic`, or `codex` for all regions; `regional` explicitly enables the site picker |
| `REVIEW_ENABLED_MODELS` | Extra configured providers available only in `regional` mode; Mock is the regional default |
| `REVIEW_MEMORY_MATCHING` | `hybrid`; `lexical` explicitly disables embeddings |
| `REVIEW_CLAIM_SECONDS` | `900`; fixed claim window |
| `REVIEW_MODEL_TIMEOUT_SECONDS` | `90`; whole review attempt budget |
| `REVIEW_MODEL_MAX_TOKENS` | `1200`; per Anthropic call |
| `REVIEW_FEED_INTERVAL_SECONDS` | `30`; synthetic arrival interval |
| `REVIEW_SESSION_SECRET` | Set a persistent secret to keep sessions across API restarts |
| `REVIEW_COOKIE_SECURE` | `false` locally; use `true` behind HTTPS |
| `REVIEW_PREVIEW_ACCESS_HASH` | Optional SHA-256 access-code verifier for a shared preview |

Keep credentials and the preview code outside source control and logs.

### Claude

To use Claude for all regions, set these values in your untracked `.env`:

```dotenv
REVIEW_MODEL_MODE=anthropic
ANTHROPIC_API_KEY=your-key-here
```

The default model is `claude-sonnet-5-5`. The official SDK has retries disabled: the worker owns the three-attempt budget.

Apply the settings to the API and workers:

```sh
docker compose up -d --force-recreate api workers
```

Global mode applies to new reviews and suggestions in every region and takes precedence over saved regional choices. The UI displays the global provider. When changing modes, let active attempts finish, then recreate the services. Restart any paused stream to resume arrivals. Use `REVIEW_MODEL_MODE=mock` to return to the offline reviewer.

### Optional Codex adapter

The image includes Codex CLI 0.166.0. To use it, authenticate its separate worker volume:

```sh
docker compose exec workers codex login --device-auth
docker compose exec workers codex login status
```

Then set `REVIEW_MODEL_MODE=codex` and recreate the API and workers using the command above. `REVIEW_CODEX_MODEL` can select a model; otherwise the CLI default is used. The adapter disables native tools, ignores personal configuration, and runs outside the repository. The CLI has its own internal harness; the application's Python loop controls claims, context, permitted lookups, validation, approval and persistence.

### Optional regional picker

For the optional site picker, set `REVIEW_MODEL_MODE=regional` and list configured providers in `REVIEW_ENABLED_MODELS`, for example `codex,anthropic`. **Save model** then changes only the current region and pauses its stream. An existing attempt keeps its provider. Saved regional choices survive global mode and become active again when regional mode is restored. Provider failures never silently fall back to Mock.

### Local development

Requires Python 3.12+, Node 22.12+, and PostgreSQL 15+ server binaries (`initdb`, `pg_ctl`) on `PATH`. Run as a regular user on Linux/macOS; Docker is the preferred Windows path.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements-dev.lock
PYTHONPATH=backend/src .venv/bin/python -m event_review.embeddings --download
npm --prefix frontend ci
npm --prefix frontend run build
.venv/bin/python scripts/dev.py serve
```

In separate terminals:

```sh
.venv/bin/python scripts/dev.py workers
npm --prefix frontend run dev
```

The helper owns a project-local PostgreSQL cluster under `.local-state/`, not your default database. The built app is served on port 8116; Vite serves hot reload on http://127.0.0.1:5193 and proxies `/api`. Restart the Python API after backend changes. The [walkthrough](docs/walkthrough.md#read-the-code-in-this-order) maps the main code paths.

## What happens to an event

1. A regional stream inserts a synthetic event. **Start stream** defaults to five minutes; arrivals continue with the browser closed. Pause stops new arrivals while queued reviews finish.
2. A worker claims it, selects eligible memory, and supplies the event and applicable site rules to the model.
3. The model returns a verdict or requests an allowed lookup. The worker executes that lookup and calls the model again, within fixed limits.
4. A valid result is saved only while the same claim is current and unexpired. The UI distinguishes a confirmed safety event, a dismissed alert, and a question needing input.
5. A supervisor saves an answer/correction. It changes that event's displayed outcome without overwriting the original review. A background model call proposes reusable guidance.
6. **Approve guidance** makes the edited wording reusable. If the model proposes no rule, the editor starts blank. UI and API reject short acknowledgements such as “yes”; the supervisor must write conditions and an outcome. Its outcome comes from the source correction. A later matching event receives it in context, and its new review can be compared with the original.

The [walkthrough](docs/walkthrough.md) covers the dated exception, 5 m separation rule, and offline severity example.

**Stop using guidance** removes that rule and its source correction from future memory selection. Both stay visible in history. Reviews already running keep their saved context.

## Context anatomy

[example-context.json](docs/example-context.json) contains the exact application inputs and outputs from a recorded Mock before/after run. [anthropic-feedback.json](docs/anthropic-feedback.json) records the same loop through the live Claude API. It includes retrieval diagnostics alongside each model-facing step; Mock is labelled explicitly. Historical traces keep the prompt version used at the time.

| Input | Why it is included |
| --- | --- |
| Instructions and response schema | Define verdicts, required fields, evidence rules, and allowed lookups |
| Event | Region, type, location, camera, occurrence time, description, detector metadata |
| Site rules | Baseline decision/severity criteria valid at the event's time, supplied upfront |
| Recent corrections | Up to three relevant case records; evidence, not automatically reusable policy |
| Approved guidance | All scoped approved rules, up to 16, with source correction and its outcome |
| Tool results and remaining budget | Evidence returned by prior lookups and whether more investigation is allowed |

Region, event type, site, zone, camera and approval status are checked before ranking. Measurements can differ from the original event. The model evaluates each approved rule's written conditions; applicable guidance overrides the baseline. Different source outcomes may cover different circumstances and do not automatically mean conflict. Cases are ranked by equal-weight word overlap and local embedding similarity: up to 64 candidates, three selected. Every scoped approved rule is supplied, up to 16; excess guidance requires supervision rather than silently omitting a rule.

The full audit retains ranking scores and selection details. `context.model_context()` removes those diagnostics from model inputs. Every call stores the exact application input before invoking the provider. Use Claude or Codex for ranges, dates and conditional rules. Mock supports the sample standing-severity sentences and asks for input on other approved text.

## API

All event/memory routes derive region from the signed session, never a client-supplied region. Cross-region IDs appear as 404. Browser requests also carry their expected supervisor; a changed session returns 412 and clears the stale workspace. Only sign-in/out sets the session cookie.

| Route | Purpose |
| --- | --- |
| `GET /api/health`, `/api/access`, `/api/demo-accounts` | Readiness, preview gate, demo identities |
| `POST/GET/DELETE /api/session` | Sign in, current identity, sign out |
| `GET /api/events` | Filters: `category`, `status`, `severity`, `event_type`, `q`; 25-event pages with `before_at`/`before_id`; returns regional counts and `next_page` |
| `GET /api/events/{id}` | Event, accepted review, attempts, exact contexts, corrections |
| `POST /api/events/{id}/retry` | Restart the budget for a failed event, retaining history |
| `POST /api/events/{id}/corrections` | Versioned supervisor answer/correction with request ID |
| `POST /api/events/{id}/simulate` | Create an event from a named evidence preset; retries use the same request ID |
| `GET/PUT /api/feed` | Inspect/start/pause the versioned regional stream |
| `PUT /api/reviewer` | Select a configured provider and pause arrivals |
| `GET /api/memory` | Corrections and proposed/approved/retired guidance |
| `POST /api/guidance/{id}/approve`, `/reject`, `/retire`, `/retry-draft` | Human memory decisions and failed-draft retry |

Desktop lists and details scroll independently. Older/Newer controls use a time/ID cursor so new arrivals do not shift older pages. Filters reset paging and keep an open supervisor draft.

**Simulate similar event** opens a chooser. `scenario` defaults to `repeat` (same evidence, current time). Access events offer `same_day` and `two_days_later`, relative to the selected event's UTC timestamp. The original `november_12` and `november_14` API presets remain valid for saved examples and evaluations. Vehicle proximity events offer `distance_3m`, `distance_4_99m`, and `distance_5m`; other metadata and location stay fixed. These are explicit synthetic inputs, not random events or expected answers. The normal worker supplies the verdict. Reusing a request ID with a different source or scenario returns 409. Unknown or incompatible scenarios return 422.

The [walkthrough](docs/walkthrough.md) covers a one-day entry exception and a stricter 5 m separation rule. Future occurrence times are labelled as tests. These examples require a real provider to interpret approved wording; Mock still demonstrates the sample severity policy.

The generated `/api/docs` describes request/response fields. Changed settings and stale correction versions return 409 rather than overwriting another supervisor's work.

## Verify

```sh
.venv/bin/python scripts/dev.py test
npm --prefix frontend exec -- playwright install chromium
.venv/bin/python scripts/dev.py browser --provider-controls --private-preview
.venv/bin/python scripts/check_model.py --mode mock
```

Backend tests use real PostgreSQL, disposable databases, restricted-role connections, and blocked outbound sockets. They cover region isolation, claims, expiry, concurrency, retries, memory, approval races, provider contracts, and uncertain saves. Provider contract tests use controlled HTTP and CLI responses.

Browser checks use disposable data and Mock workers. They cover responsive navigation, answers, corrections, approval, later reviews, filters, provider selection, outages, stale edits, and lost acknowledgements. Hosted HTTPS checks run separately from the local browser suite. Screenshots go under ignored `.work/`; `PLAYWRIGHT_CHROMIUM_EXECUTABLE` can select an installed browser.

Container backend check:

```sh
docker compose --profile test run --build --rm test
```

For a live-provider feedback check, use `scripts/check_model.py --mode anthropic` or `--mode codex`. These make real calls, retain input/output evidence, and remove their disposable database afterward.

See the [verification record](docs/verification.md) for completed checks and the environments used.

The Docker acceptance check uses fresh volumes to verify keyless startup, feedback and restart persistence across all seven regional tables. It removes its test stack afterward:

```sh
python3 scripts/check_compose.py --output .work/compose-check
```

It requires a Docker engine and an unused loopback port (default 8117).

### Track model cost

Tokens are saved for each review call and guidance draft. Estimate Claude spending from those records without making model calls:

```sh
# Uses REVIEW_PGHOST / REVIEW_PGPORT / REVIEW_PGDATABASE for your database.
.venv/bin/python scripts/check_usage.py --database --output .work/usage.json
# Include saved disposable-test reports when calculating total project use.
.venv/bin/python scripts/check_usage.py .work/my-evaluation.json --output .work/test-usage.json
```

The report deduplicates repeated snapshots, includes usage from failed verdicts and uses the linked Sonnet 5.5 list prices checked on November 13, 2026. Unknown models or unsupported cache-write pricing are reported as unpriced. This is a recorded-usage estimate, not an account balance; a request whose usage response was lost cannot be counted. Mock has no API charge, and Codex subscription usage is separate.
