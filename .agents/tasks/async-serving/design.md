# Async 202 + Poll Serving Path — Requirements & Design

## Requirements

### Summary

The MovieIntel serving API returns an intermittent, unstructured `503 {"message":"Service Unavailable"}`
on `POST /query`. The root cause is confirmed live: the API Gateway v2 HTTP API integration
timeout is hard-capped at 30s, while the synchronous handler runs the multi-turn Bedrock
Converse agent loop (`run_agent`) that sometimes exceeds 30s (observed Lambda invocations of
30.6s and 31.7s with `IntegrationLatency` pinned at 30000ms and matching `5xx` counts). The
gateway returns 503 at the 30s wall even though the Lambda (300s timeout) finishes in the
background, which is why a retry usually works.

This feature eliminates the 30s-wall 503 by decoupling the long-running agent loop from the
request/response window using an **async submit-then-poll** path on the **same API Gateway v2
HTTP API** (OPTION C — decided, not relitigated). A submit route returns `202` quickly (well
under 30s) with a job id and poll URL; a worker Lambda runs the loop to completion and writes
status, **real per-phase progress**, and the terminal result into DynamoDB; a poll route
returns a fast single-item read. The front end moves from a single blocking fetch to
submit-then-poll with real progress rendered into the existing aria-live region.

No Lambda Function URL is introduced and API Gateway is not replaced. The agent loop's
control flow and the 5-kind `AgentResult` contract are unchanged; only an optional,
backward-compatible progress callback is added. `POST /query`'s 400-validation behavior and
the 5-kind result shapes are preserved. The separate parked `fix/serving-max-turns-clamp`
branch is not depended on or merged.

### Functional Requirements

- **FR-1 Submit route.** A new `POST /jobs` route on the existing HTTP API accepts the exact
  `QueryRequest` body shape (`query` required non-empty; optional `max_turns` in `[1, 12]`),
  validates it with the same strictness as `POST /query` (`extra='forbid'`), persists a new
  job record with status `queued`, asynchronously invokes the worker, and returns `202` with
  `{job_id, status, poll_url}` well under 30s. A schema-violating body returns the existing
  structured `400 ValidationErrorResponse` without creating a job or invoking the worker.
- **FR-2 Poll route.** A new `GET /jobs/{id}` route returns the current job state as a single
  fast DynamoDB `GetItem`: `{job_id, status, progress, created_at, updated_at}` plus, when
  terminal, either the full serialized 5-kind `AgentResult` (`result`) or a structured
  `error`. An unknown job id returns `404`. Status is one of
  `queued | running | succeeded | failed`.
- **FR-3 Per-phase progress.** The poll response carries a real `progress` object
  `{phase, turn, label}` reflecting the actual agent-loop position (not a timer), updated by
  the worker as the loop advances through understanding, searching, comparing, and composing.
- **FR-4 Worker.** A new worker Lambda runs `run_agent` to completion, writing progress
  checkpoints and a terminal record. It MUST write a terminal record even on an unexpected
  exception (mapped to `status=failed`) so a poller never hangs.
- **FR-5 Optional progress callback.** `run_agent` gains a keyword-only `on_progress`
  callback (default `None`). When `None`, control flow, return value, and all existing
  behavior are byte-for-byte identical. When supplied, it emits typed `ProgressEvent`s at
  loop start, per turn, per tool dispatch, guardrail refusal, final answer, and bounded.
- **FR-6 `POST /query` preserved.** The synchronous `POST /query` route and `GET /health`
  route remain exactly as they are today (back-compatible). Direct non-browser clients can
  still call `POST /query` as the README documents.
- **FR-7 Front end.** `frontend/app.js` moves from single-fetch to submit-then-poll with real
  progress rendered via `renderNotice(..., 'progress')` into the existing aria-live region,
  preserving `renderResult` and all `render*` helpers (textContent-only XSS boundary)
  unchanged for the terminal result.
- **FR-8 Docs.** `docs/openapi.yaml` is the authoritative contract and documents the new
  routes. `docs/examples/{curl.sh,postman_collection.json}` are runnable submit-then-poll
  flows. The README serving/query flow and section 8 (503) reflect that async removes the
  30s-wall 503.
- **FR-9 Job TTL.** Job records carry a DynamoDB TTL attribute so they expire automatically.

### Non-Functional Requirements

- **NFR-1 Least privilege.** New IAM is scoped to the exact job keyspace and the exact
  Bedrock/KB/DynamoDB resources already used by serving. No `bedrock:*`, no `Resource: "*"`.
- **NFR-2 Scale-to-zero.** No provisioned concurrency on submit or worker Lambdas.
- **NFR-3 CORS + WAF preserved.** CORS preflight works for the new routes; the existing WAF
  WebACL arrangement (defined unconditionally, association gated by `associate_waf`) is kept.
- **NFR-4 AWS steering.** Hyphens, never em dashes, in resource names/descriptions; IaC via
  CDK; Well-Architected.
- **NFR-5 CI clean.** `ruff check`, `ruff format --check`, `mypy src`, and `pytest` pass
  (exact commands in the design). Pre-existing `_sqlite`-dependent test failures
  (`tests/data_access`, `test_extract_handler`) are UNRELATED and not regressions.

### Acceptance Criteria

1. `POST /jobs` with a valid body returns `202` in under 30s (far under, since it does not run
   the loop) with a JSON body containing a non-empty `job_id`, `status: "queued"`, and a
   `poll_url` ending in `/jobs/{job_id}`.
2. `POST /jobs` with a body missing `query`, with an empty `query`, with `max_turns` outside
   `[1,12]`, or with an unknown field returns `400` with `{"error":"invalid_request","detail":[...]}`
   and creates no job record.
3. `GET /jobs/{id}` for a known in-flight job returns `200` with `status` in
   `{queued, running}` and a `progress` object `{phase, turn, label}`.
4. `GET /jobs/{id}` for a completed job returns `200` with `status: "succeeded"` and a
   `result` that is a valid 5-kind `AgentResult` (including `refusal`/`bounded` as success
   outcomes). A `JobStore` round-trip storing a `RecommendationList` with a `float` `pes` and a
   `progress.turn` of `2` reads back a `JobStatusResponse` whose `result` equals the original and
   whose `progress.turn == 2` is an `int` (the read-path projection excludes the item's
   `PK`/`SK`/`query`/`max_turns`/`expires_at`, reconstructs `job_id` from the key, and normalizes
   `progress`/`result`/`error` through `_from_dynamo`).
5. `GET /jobs/{id}` for a job whose worker raised an unexpected exception returns `200` with
   `status: "failed"` and a structured `error` object; the record exists (the poller never
   hangs).
6. `GET /jobs/{id}` for an unknown id returns `404`.
7. `run_agent(...)` called WITHOUT `on_progress` produces identical results to today for every
   existing test (the agent-loop test suite passes unchanged).
8. `run_agent(..., on_progress=cb)` invokes `cb` with a `ProgressEvent` at: loop start, each
   turn, each tool dispatch (naming the tool), a guardrail refusal, a final answer, and the
   bounded outcome.
9. The worker writes a terminal record (`succeeded`/`failed`) for every job, including on an
   unhandled exception inside `run_agent`.
10. `POST /query` and `GET /health` behave exactly as before (existing serving-handler tests
    pass unchanged).
11. `docs/openapi.yaml` documents `POST /jobs`, `GET /jobs/{id}`, and `POST /query` (matching the
    current generator, which emits `/query` only and intentionally omits `GET /health`); the
    on-disk file is in sync with the generator (`test_on_disk_openapi_is_in_sync`).
12. The front end submits to `POST /jobs`, polls `GET /jobs/{id}` at ~1-1.5s with a cap and
    backoff, renders real progress into the aria-live region, and renders the terminal result
    via the unchanged `renderResult`; the submit button is disabled in-flight and re-enabled
    in `finally`; timers are cleared on terminal/error.
13. `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, and
    `uv run pytest` succeed (modulo the pre-existing unrelated `_sqlite` failures).
14. `npx aws-cdk@2 synth MovieIntelServingStack` succeeds from `infra/`.

### Out of Scope

- Changing the agent loop's control-flow or result semantics (only the optional callback is
  added).
- Introducing a Lambda Function URL or removing/replacing API Gateway.
- Changing `POST /query`'s 400-validation behavior or the 5-kind `AgentResult` shapes.
- Response streaming (SSE) — the parked alternative, not this path.
- Depending on or merging `fix/serving-max-turns-clamp`.
- Merging or deploying (authoring only).
- Authentication/authorization on the new routes beyond the existing WAF rate limit.

---

## Design

### Overview

We add an async submit-then-poll path to the existing `MovieIntelServingStack`
(`infra/serving_stack.py`) without touching the synchronous `POST /query` or `GET /health`
routes. Two new routes are added to the same HTTP API (`izizrxgasb`): `POST /jobs` (submit)
and `GET /jobs/{id}` (poll). `POST /jobs` is handled by a new, thin submit handler that reuses
the existing request validation, writes a `queued` job record to the existing `MovieIntel`
DynamoDB table under a dedicated `JOB#` keyspace, asynchronously invokes a new worker Lambda
(`boto3` `lambda.invoke(InvocationType='Event')`), and returns `202`. The worker Lambda runs
`run_agent` to completion, streaming real per-phase progress into the job record via a
backward-compatible `on_progress` callback, and writes the terminal `AgentResult` (or a
structured error on crash). `GET /jobs/{id}` is a fast single `GetItem`.

The agent loop (`src/movieintel/agent/loop.py`) is extended ONLY with a keyword-only
`on_progress: Callable[[ProgressEvent], None] | None = None` parameter; when `None` the loop
is byte-for-byte identical to today. The front end is rewritten from single-fetch to
submit-then-poll, preserving the `renderResult`/`render*` XSS boundary.

**Technology stack (locked):** Python 3.12; `pydantic>=2.7`; `boto3>=1.34`;
`aws-cdk-lib==2.272.0` with `aws_apigatewayv2` + `aws_apigatewayv2_integrations`,
`aws_lambda`, `aws_iam`, `aws_dynamodb` (for the TTL attribute enablement), `aws_bedrock`,
`aws_wafv2`; DynamoDB single `MovieIntel` table (reused, PAY_PER_REQUEST); vanilla ES
front end (no build step). Tooling: `uv`, `ruff`, `mypy`, `pytest`, `moto`. CDK synth via
`npx aws-cdk@2 synth`.

### Decision 1 — Async job contract on the same HTTP API

**Decision: ADD a new `POST /jobs` route; keep `POST /query` synchronous and unchanged.**

Three options were considered: (a) replace `POST /query` semantics with async; (b) add a new
`POST /jobs` route and leave `POST /query` as-is; (c) make `POST /query` async while keeping
back-compat via a flag/header. We choose **(b)**. Rationale: the README, `curl.sh`,
`postman_collection.json`, and `test_handler.py` all document and test `POST /query` as a
synchronous route returning the serialized `AgentResult` at 200; direct non-browser clients
depend on that. Option (a) breaks every existing client and contract test. Option (c) overloads
one route with two response shapes (202 vs 200) keyed on a side-channel, which is harder to
document in OpenAPI and easy to misuse. A dedicated `POST /jobs` + `GET /jobs/{id}` pair is the
cleanest contract, leaves the synchronous path intact as a fallback, and is what the front end
and new examples target. The intermittent 503 remains possible on the *synchronous* `POST /query`
by design (it is the legacy path); the async path is the 503-free route and is what the front
end and docs steer users to.

**Routes (all on the existing `ServingHttpApi`, `$default` stage, no stage path):**

| Method | Path          | Handler        | Purpose                                   |
|--------|---------------|----------------|-------------------------------------------|
| POST   | `/query`      | `ServingFn`    | Unchanged synchronous path (legacy)       |
| GET    | `/health`     | `ServingFn`    | Unchanged warmup short-circuit            |
| POST   | `/jobs`       | `SubmitFn`     | Create job, async-invoke worker, 202      |
| GET    | `/jobs/{id}`  | `SubmitFn`     | Poll job state (single GetItem)           |

`POST /jobs` and `GET /jobs/{id}` share ONE new Lambda (`SubmitFn`) via a single
`HttpLambdaIntegration`, mirroring how `POST /query` and `GET /health` share `ServingFn`. The
worker Lambda (`WorkerFn`) is NOT wired to any API route; it is invoked only asynchronously by
`SubmitFn`.

**Route dispatch contract (pinned — do NOT copy `_is_health_request`'s exact-string match).**
`_is_health_request` (verified in `infra/handlers/serve/handler.py`) matches an EXACT, unparameterized
path (`GET /health`). `GET /jobs/{id}` is a PARAMETERIZED v2 route: on the HTTP API v2 proxy event,
`event["routeKey"]` is the literal TEMPLATE `"GET /jobs/{id}"` while
`event["requestContext"]["http"]["path"]` is the RESOLVED path `"/jobs/<uuid>"`. A naive
`requestContext.http.path == "/jobs/{id}"` (or `== "/jobs"`) comparison would never match the
resolved path. The submit handler therefore dispatches with these exact rules, checked in order:

- **Submit — `POST /jobs`:** `event.get("routeKey") == "POST /jobs"` OR
  (`requestContext.http.method == "POST"` AND `requestContext.http.path == "/jobs"`).
- **Poll — `GET /jobs/{id}`:** `event.get("routeKey") == "GET /jobs/{id}"` OR
  (`requestContext.http.method == "GET"` AND `requestContext.http.path.startswith("/jobs/")`).
  The poll branch calls `JobStore.get(job_id)`; `None` → `404 NotFoundResponse`, a found record →
  `200 JobStatusResponse`. **A `JobStore.get` that RAISES (a DynamoDB read failure) is caught and
  returned as `500 {"error":"poll_failed","message":...}`, logged at `error`** (matching the
  error-handling table). This is the only `500` on the poll route; the front-end poll loop treats
  `>= 500` as retry-with-backoff.
- **Anything else:** return a `404 NotFoundResponse` (the handler is only wired to the two `/jobs`
  routes, so this is defensive).

**Reading `{id}`:** the handler reads `event["pathParameters"]["id"]`. The defensive fallback (when
`pathParameters` is absent/empty) parses the LAST path segment of the RESOLVED
`requestContext.http.path` — i.e. `path.rstrip("/").rsplit("/", 1)[-1]` — NOT a comparison against the
`"{id}"` template. An empty/missing id is treated as `404` (see Decision 1 validation rules). These
rules are asserted directly in the submit-handler unit test (Testability section): a `POST /jobs`
event routes to submit, a `GET /jobs/{id}` event with both `routeKey` and resolved-path forms routes
to poll and extracts the id, and a mismatched event returns `404`.

**`POST /jobs` request body** — identical to `QueryRequest` (reused verbatim):

```json
{ "query": "Recommend action movies ...", "max_turns": 8 }
```

(The `8` is an illustrative override value within `[1, 12]`, NOT a new default — the default
remains `MAX_TURNS = 6`; the worker falls back to `MAX_TURNS` when `max_turns` is absent, matching
the serve handler's `max_turns=request.max_turns or MAX_TURNS` exactly.)

`query` required, non-empty; `max_turns` optional, integer in `[1, 12]` (`MAX_TURNS_CAP`);
`extra='forbid'`. A schema-violating body returns the existing `ValidationErrorResponse` at
`400` (reuse the serve handler's `_parse_request`/`_bad_request`/`_decode_error` logic,
factored into a shared module — see Decision 3's handler layout).

**`job_id` form (pinned, used everywhere).** The submit handler generates
`job_id = uuid4().hex` — a 32-char lowercase hex string with NO dashes (NOT `str(uuid4())`). The
item PK is `job_pk(job_id)` = `JOB#<hex>`; `poll_url = f"/jobs/{job_id}"`. All JSON examples in
this design (and the OpenAPI/curl/postman docs) use the hex form so the example, the `job_pk`
input, and the `poll_url` always agree; a dashed-uuid example would 404 on poll because the PK
encodes the hex form. No dashed-uuid examples remain.

**`POST /jobs` success response** — `202`:

```json
{ "job_id": "0f9d2c6e4a1b4c2e9f3d7a6b5c8e1d0f", "status": "queued", "poll_url": "/jobs/0f9d2c6e4a1b4c2e9f3d7a6b5c8e1d0f" }
```

`poll_url` is a relative path (the API has no fixed stage path and the base URL is the client's
own `API_BASE`); the front end and examples construct the absolute URL by prefixing `API_BASE`.

**`GET /jobs/{id}` response** — `200` (in-flight and terminal) / `404` (unknown):

In-flight (`queued`/`running`):
```json
{
  "job_id": "0f9d2c6e4a1b4c2e9f3d7a6b5c8e1d0f",
  "status": "running",
  "progress": { "phase": "searching", "turn": 2, "label": "searching the catalog" },
  "created_at": "2025-01-01T00:00:00Z",
  "updated_at": "2025-01-01T00:00:03Z"
}
```

Terminal success (`succeeded`) — `result` is the serialized 5-kind `AgentResult`:
```json
{
  "job_id": "0f9d2c6e4a1b4c2e9f3d7a6b5c8e1d0f",
  "status": "succeeded",
  "progress": { "phase": "composing", "turn": 3, "label": "composing the answer" },
  "created_at": "...", "updated_at": "...",
  "result": { "kind": "recommendations", "query_summary": "...", "movies": [ ... ] }
}
```
Note: `refusal` and `bounded` are `AgentResult` members, so a guardrail refusal or a max-turns
bound is `status: "succeeded"` with `result.kind` = `refusal`/`bounded` (they are successful,
policy-compliant outcomes, exactly as the synchronous path returns them at HTTP 200).

Terminal failure (`failed`) — only for infrastructure/unexpected errors (never a modeled agent
outcome):
```json
{
  "job_id": "0f9d2c6e4a1b4c2e9f3d7a6b5c8e1d0f",
  "status": "failed",
  "progress": { "phase": "...", "turn": N, "label": "..." },
  "created_at": "...", "updated_at": "...",
  "error": { "error": "worker_failed", "message": "unexpected error running the agent" }
}
```

Unknown id — `404`:
```json
{ "error": "not_found", "message": "no job with that id" }
```

**New Pydantic models** (new module `src/movieintel/serving/jobs.py`, strict `extra='forbid'`,
single source of truth for OpenAPI generation):

- `JobStatus` — `enum.StrEnum` with members `queued`, `running`, `succeeded`, `failed`.
- `ProgressPhase` — `enum.StrEnum` (see Decision 4): `understanding`, `searching`, `comparing`,
  `composing`, `refused`, `bounded`.
- `JobProgress` — `{ phase: ProgressPhase, turn: int, label: str }`.
- `SubmitResponse` — `{ job_id: str, status: Literal["queued"] = "queued", poll_url: str }`.
- `JobStatusResponse` — `{ job_id: str, status: JobStatus, progress: JobProgress,
  created_at: str, updated_at: str, result: AgentResult | None = None,
  error: WorkerError | None = None }` (serialized with `exclude_none=True` so `result`/`error`
  appear only when terminal). **`progress` is REQUIRED (non-optional).** The read path does NOT
  `model_validate` the raw item — it validates an EXPLICIT PROJECTED payload (see "`JobStore.get`
  read-path projection" in Decision 2) that excludes the item's extra attributes
  (`PK`/`SK`/`query`/`max_turns`/`expires_at`) and reconstructs the required `job_id` from the key,
  so `extra='forbid'` is safe. Combined with the invariant below (every write path preserves a
  complete `progress` map), a poll of a healthy job never 500s on validation.
  **Terminal-failure nesting (intentional):** because `error: WorkerError | None` embeds the full
  `WorkerError` object, a `failed` body serializes with the `WorkerError` NESTED under the
  top-level `error` key — `{"status":"failed","error":{"error":"worker_failed","message":...}}` — a
  doubly-nested `error`. This is deliberate (matches the failure example, keeps `WorkerError`
  self-describing); the front-end `failed` branch and OpenAPI authors MUST NOT flatten it.

**`progress`-always-present invariant.** `JobStore.create` ALWAYS writes a complete initial
`progress` map (`{phase: understanding, turn: 0, label: PHASE_LABELS[understanding]}`), and NO
write path ever removes `progress`: `mark_running` touches only `status`/`updated_at`;
`update_progress` overwrites `progress` with a complete `JobProgress`; `mark_succeeded`/
`mark_failed` touch only `status`/`result`/`error`/`updated_at`. Therefore any readable job item
(including one marked `failed` by the submit handler's async-invoke-failure path, which runs after
`create`, and one whose worker crashed before the first `on_progress`) always carries a complete
`progress`. The poller never 500s on a missing-`progress` validation error. (A `failed` job simply
shows its last-known progress — the `create`-time `understanding/0` if the worker never advanced.)
- `WorkerError` — `{ error: Literal["worker_failed"] = "worker_failed", message: str }`.
- `NotFoundResponse` — `{ error: Literal["not_found"] = "not_found", message: str }`.

The `AgentResult` union is reused verbatim from `movieintel.agent.response`; `JobStatusResponse`
embeds it as `result: AgentResult | None`. `AgentResult` is a PEP 695 `type` alias whose five
members are disjoint-`kind` `extra='forbid'` models; Pydantic v2 (>=2.7, pinned) resolves the alias
as a field type and validates `result` via SMART-UNION on the disjoint `kind` discriminator (no
`TypeAdapter`, no explicit `Field(discriminator=...)`). This is the first use of `AgentResult` as a
Pydantic FIELD; serializing it into a job record and back out is lossless (`model_dump(mode="json")`
/ `model_validate`), proven by the `JobStore` round-trip test (Testability) that stores a
`RecommendationList` with a `pes` float and asserts equality on read (the alias-as-field proof).

CORS, WAF: the new routes inherit the existing `CorsPreflightOptions` (methods GET/POST/OPTIONS
already allowed; `content-type`/`authorization` headers already allowed) because CORS preflight
is configured at the HTTP API level, not per route — no change needed. The WAF WebACL and its
`associate_waf`-gated association are unchanged and already apply to the whole `$default` stage.

### Decision 2 — Job store in DynamoDB (existing `MovieIntel` table)

**Decision: reuse the existing `MovieIntel` table with a dedicated `JOB#` keyspace and a TTL
attribute. Do NOT create a separate jobs table.**

Rationale: the table is already PAY_PER_REQUEST (scale-to-zero), already deployed, and the
single-table design (`src/movieintel/persistence/keys.py`) is built to overload one physical
schema with typed prefixes (`MOVIE#`, `SENTIMENT#`). A `JOB#` prefix is the idiomatic
extension. A separate table would add a second resource, a second set of IAM ARNs, and a second
CDK construct for no benefit at this scale (jobs are short-lived, low-volume). The job keyspace
is fully disjoint from the movie keyspace, so there is no read/write interference and no new GSI
is required (poll is a direct `GetItem` by PK/SK).

**Job item key schema** (new helpers in `src/movieintel/persistence/keys.py`):

| Attribute   | Value                        | Notes                                           |
|-------------|------------------------------|-------------------------------------------------|
| `PK`        | `JOB#<job_id>`               | `job_pk(job_id)`; `job_id = uuid4().hex` (lowercase, no dashes) |
| `SK`        | `JOB`                        | `job_sk()`; fixed sentinel (mirrors `META`)     |
| `status`    | `queued`\|`running`\|`succeeded`\|`failed` | top-level string attribute          |
| `progress`  | `{ phase, turn, label }`     | DynamoDB map                                    |
| `query`     | the input query string       |                                                 |
| `max_turns` | int or absent                | the optional override                           |
| `created_at`| ISO-8601 UTC `Z` string      |                                                 |
| `updated_at`| ISO-8601 UTC `Z` string      | advanced on every write                         |
| `result`    | serialized `AgentResult` map | present only when `status=succeeded`            |
| `error`     | `{ error, message }` map     | present only when `status=failed`               |
| `expires_at`| epoch-seconds number (TTL)   | `ttl_epoch_seconds()`; `now + JOB_TTL_SECONDS`  |

New keys-module helpers (alongside `movie_pk`/`meta_sk`):

```python
JOB_SK = "JOB"


def job_pk(job_id: str) -> str:
    return f"JOB#{job_id}"


def job_sk() -> str:
    return JOB_SK
```

**TTL:** add a `JOB_TTL_SECONDS` constant (default `86400` = 24h) in a new
`src/movieintel/serving/jobs_store.py` (see below). The item's `expires_at` =
`int(time.time()) + JOB_TTL_SECONDS`. **The table is created in the pipeline stack but is NOT a
cross-stack reference in serving** (serving pins the table name as a literal). Enabling TTL is a
table-level property; the serving stack does not own the table resource.

**Decision: enable TTL on the table in `infra/pipeline_stack.py`, where it is defined.** The
construct is already known (verified): `_dynamodb_table()` builds
`dynamodb.Table(self, "MovieIntelTable", table_name=self.persistence.table_name,
partition_key=..., sort_key=..., billing_mode=PAY_PER_REQUEST, removal_policy=DESTROY)` and then
adds GSI1. `aws_dynamodb.Table` accepts `time_to_live_attribute` directly, so the change is
deterministic, not a "verify": **add `time_to_live_attribute="expires_at"` to that existing
`dynamodb.Table(...)` call.** TTL is idempotent and backward-compatible — existing movie items
simply have no `expires_at` and never expire; this is co-located with the table definition and
adds no cross-stack dependency.

Because every enumerated serving-stack synth assertion targets `MovieIntelServingStack`, the TTL
change on the pipeline stack is otherwise untested. **Add one assertion to the existing
`tests/infra/test_pipeline_stack_synth.py`** on the `AWS::DynamoDB::Table` resource's
`TimeToLiveSpecification`: `{ "AttributeName": "expires_at", "Enabled": true }`, so the TTL enablement
is locked by a test rather than only by code review.

**Job-store access layer** — new `src/movieintel/serving/jobs_store.py`, a thin wrapper over an
injected boto3 DynamoDB `Table` resource (same injection pattern as `MovieIntelRepository`), so
both the submit handler and worker use one tested API and tests use a moto table:

```python
class JobStore:
    def __init__(self, *, table: Any, config: PersistenceConfig) -> None: ...
    def create(self, *, job_id: str, query: str, max_turns: int | None) -> None:
        # PutItem: status=queued, progress={phase:understanding,turn:0,
        #   label:PHASE_LABELS[understanding]} (ALWAYS a COMPLETE JobProgress map),
        # created_at=updated_at=now, expires_at=now+TTL. ConditionExpression
        # attribute_not_exists(PK) so a duplicate id cannot clobber.
    def mark_running(self, job_id: str) -> None:
        # UpdateItem SET status=running, updated_at=:t. This is the SOLE owner of the
        # queued -> running transition (see "Status ownership" below). Idempotent:
        # ConditionExpression status IN (:queued,:running) so it never reverses a terminal.
    def update_progress(self, job_id: str, progress: JobProgress) -> None:
        # UpdateItem SET progress=:p, updated_at=:t. Does NOT touch `status` (does not
        # flip to running). ConditionExpression status IN (:queued,:running) so a stray
        # late progress write can never overwrite a terminal record (see Finding 4 fix).
    def mark_succeeded(self, job_id: str, result: AgentResult) -> None:
        # UpdateItem SET status=succeeded, result=:r, updated_at=:t. Terminal write.
        # ConditionExpression status IN (:queued,:running) so a terminal is set at most once.
    def mark_failed(self, job_id: str, error: WorkerError) -> None:
        # UpdateItem SET status=failed, error=:e, updated_at=:t. Terminal write.
        # ConditionExpression status IN (:queued,:running).
    def get(self, job_id: str) -> JobStatusResponse | None:
        # GetItem PK/SK; None when absent (poll -> 404). Builds an EXPLICIT PROJECTED
        # payload of ONLY JobStatusResponse model fields (see "Read-path projection"
        # below) before model_validate — never validates the raw item.
```

Writes use `UpdateItem` with explicit `UpdateExpression`/`ExpressionAttributeNames/Values` so a
progress update never rewrites the whole item (and never races the result write across
attributes). Numbers (`max_turns`, `expires_at`, `turn`) are stored as DynamoDB numbers.

**Float handling for the `result` map — exact technique (DO NOT assume the repository already does
this).** The DynamoDB boto3 `Table` resource rejects Python `float`s; it requires `Decimal`. The
`AgentResult` union can contain floats (e.g. a recommendation movie's `pes`). The repository's
own serializer (`EnrichedMovie.to_item` in `src/movieintel/persistence/item.py`) handles this by
converting each known float FIELD-BY-FIELD via `Decimal(str(value))` and exposes an `_as_number`
helper (`repository.py`) for scalar query bounds — it does NOT perform a generic JSON round-trip,
and `JobStore` must NOT try to reuse `_as_number`/`to_item` for the arbitrary nested `AgentResult`
map. Instead, `JobStore` uses the standard generic technique for an arbitrarily-shaped map:

```python
from decimal import Decimal
import json


def _to_dynamo(model: BaseModel) -> dict:
    # pydantic -> JSON-safe dict -> JSON text -> reload coercing every float to Decimal.
    return json.loads(json.dumps(model.model_dump(mode="json")), parse_float=Decimal)


def _from_dynamo(raw: dict) -> dict:
    # Decimal -> JSON-safe (Decimal is not json-serializable by default), reload as float.
    return json.loads(json.dumps(raw, default=float))
```

`mark_succeeded` stores `_to_dynamo(result)`. The read path (`get`) does NOT validate the raw
item — see the explicit projection below. This keeps DynamoDB happy and the pydantic models
authoritative, and is self-contained in `jobs_store.py` (it does not depend on repository
internals). The coder should confirm `EnrichedMovie.to_item`'s approach matches this description
(it does, as of this writing) but implement the generic `_to_dynamo`/`_from_dynamo` for jobs
rather than reusing movie-specific serialization.

**`JobStore.get` read-path projection (REQUIRED — the stored item is NOT a `JobStatusResponse`).**
The stored DynamoDB job item carries attributes that are NOT fields of the strict
(`extra='forbid'`) `JobStatusResponse` model: `PK`, `SK`, `query`, `max_turns`, and `expires_at`.
Worse, the item stores NO `job_id` attribute at all — `job_id` lives ONLY inside `PK` as
`JOB#<job_id>` — yet `job_id` is a REQUIRED field of `JobStatusResponse`. Therefore
`JobStatusResponse.model_validate(raw_item)` would raise a `ValidationError` (extra attributes
rejected AND the required `job_id` missing), 500-ing EVERY poll of a healthy job. `get` MUST
instead build an EXPLICIT PROJECTED payload of only model fields, reconstructing `job_id` from the
METHOD ARGUMENT (never from the item), copying only `status`/`progress`/`created_at`/`updated_at`,
and conditionally adding `result`/`error`, then validating THAT payload. `PK`, `SK`, `query`,
`max_turns`, and `expires_at` are EXCLUDED before validation. Because DynamoDB returns every number
as `Decimal` (including the NESTED `progress.turn`, and any `pes` inside `result`), `progress` AND
`result` AND `error` are ALL passed through `_from_dynamo` so no stray `Decimal` reaches the model:

```python
def get(self, job_id: str) -> JobStatusResponse | None:
    resp = self._table.get_item(Key={"PK": job_pk(job_id), "SK": job_sk()})
    item = resp.get("Item")
    if item is None:
        return None  # poll -> 404
    payload: dict[str, Any] = {
        "job_id": job_id,  # reconstructed from the ARG, not stored
        "status": item["status"],
        "progress": _from_dynamo(item["progress"]),  # Decimal turn -> int
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }
    if "result" in item:
        payload["result"] = _from_dynamo(item["result"])  # Decimal pes -> float
    if "error" in item:
        payload["error"] = _from_dynamo(item["error"])
    return JobStatusResponse.model_validate(payload)  # ONLY model fields -> extra='forbid' is safe
```

Passing `progress` through `_from_dynamo` is the explicit, complete `Decimal` story (do NOT rest
on an implicit Pydantic `Decimal->int` coercion for `progress.turn`): `_from_dynamo` normalizes the
whole map so the model always receives JSON-native numbers. The `JobStore` round-trip test
(Testability) asserts both `get(...).progress.turn == 2` after `update_progress(turn=2)` AND that
`get(...).result` equals a stored `RecommendationList` carrying a `float` `pes`, locking the
projection and the `Decimal`/float normalization.

**`result: AgentResult | None` validation technique.** `AgentResult` is a PEP 695 `type` alias
(`type AgentResult = RecommendationList | ... | RefusalResponse`) whose five members are all
`extra='forbid'` `BaseModel`s with DISJOINT `kind` literals. Pydantic v2 (>=2.7, pinned) resolves a
PEP 695 alias as a field type, so `JobStatusResponse.model_validate(payload)` validates
`payload["result"]` via Pydantic SMART-UNION on the disjoint `kind` discriminator — NO
`Field(discriminator=...)` and NO `TypeAdapter` are needed (the disjoint `kind` literals make the
left-to-right best-match unambiguous). This is the first use of `AgentResult` as a Pydantic FIELD
(it is used elsewhere only as a return annotation); the `JobStore` round-trip test storing a
`RecommendationList` with a `pes` float and asserting `get(...).result == original` is the explicit
alias-as-field proof.

### Decision 3 — Worker invocation mechanism

**Decision: the submit handler asynchronously invokes a dedicated worker Lambda via
`boto3` `lambda_client.invoke(InvocationType='Event', ...)`. Not Step Functions, not a
self-async pattern.**

Rationale: there is exactly one long-running task (one `run_agent` call) per request. Step
Functions adds a state machine, execution history, and IAM for orchestration we do not need —
overkill for a single task (the tradeoff: SFN would give built-in retries/visibility, but we
get sufficient visibility from the job record's progress and a terminal `failed` status, and
retries are undesirable for a non-idempotent Bedrock loop). A Lambda "self-async" pattern
(the same function re-invoking itself) couples submit latency and worker IAM into one function
and muddies the least-privilege boundary. A dedicated worker Lambda cleanly separates the
fast, low-privilege submit path from the slow, Bedrock-privileged worker path, reuses the
existing shared layer + handler asset layout, and gets the full 300s Lambda timeout for the
loop. `InvocationType='Event'` returns immediately (fire-and-forget) so `POST /jobs` stays well
under 30s.

**Invocation call** (in the submit handler):

```python
import boto3, json

lambda_client = boto3.client("lambda", region_name=region)
lambda_client.invoke(
    FunctionName=os.environ["WORKER_FUNCTION_NAME"],
    InvocationType="Event",
    Payload=json.dumps({"job_id": job_id, "query": query, "max_turns": max_turns}).encode(),
)
```

Ordering: the submit handler `JobStore.create(...)` **before** invoking the worker, so a
`queued` record always exists before the async dispatch — a poll immediately after `202` always
finds the record. If the async `invoke` call itself fails (rare — throttling/permission), the
submit handler marks the job `failed` with `WorkerError(message="could not start worker")` and
still returns `202` with the job id (the poller will see `failed`), OR returns a `500` — 
**Decision: mark the job `failed` and return `202`**, so the client has a job id to poll and a
uniform terminal path, rather than two different submit-error shapes. (A `500` is reserved for
the submit handler being unable to even write the `queued` record.)

**Worker handler** — new `infra/handlers/worker/handler.py`
(handler string `handlers.worker.handler.handler`), mirroring the serve handler's dependency
injection:

```python
def handler(
    event,
    context,
    *,
    client=None,
    repository=None,
    kb_client=None,
    config=None,
    persistence=None,
    kb_config=None,
    job_store=None,
) -> dict:
    job_id = event["job_id"]
    query = event["query"]
    max_turns = event.get("max_turns")
    # build deps lazily (same _default_* builders as serve handler, shared module)
    job_store.mark_running(job_id)
    try:
        on_progress = _progress_writer(job_store, job_id)  # throttled DynamoDB writer
        result = run_agent(
            query,
            client=...,
            repository=...,
            kb_client=...,
            kb_config=...,
            config=...,
            max_turns=max_turns or MAX_TURNS,
            on_progress=on_progress,
        )
        job_store.mark_succeeded(job_id, result)
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        logger.exception("worker failed for job %s", job_id)
        job_store.mark_failed(job_id, WorkerError(message="unexpected error running the agent"))
    return {"job_id": job_id}
```

The broad `except Exception` is deliberate and is the ONLY place a broad catch is allowed: it
guarantees FR-4 (a terminal record always exists). `run_agent` itself never raises for a
per-request problem (guardrail/bounded/unknown-tool are structured outcomes), so this catch only
fires on genuine infrastructure errors (Bedrock throttling exhausted, DynamoDB failure, etc.).
The exception is logged at `exception` level (full traceback) in CloudWatch; the caller
(poller) receives a generic `worker_failed` message with no internal detail (no secret/stack
leakage). The worker's return value is ignored (async invoke).

**Shared handler helpers.** The `_default_bedrock_client`/`_default_kb_client`/
`_default_repository`/`_decode_body`/`_parse_request`/`_bad_request`/`_decode_error` logic is
currently private to `infra/handlers/serve/handler.py`. Factor the reusable pieces into
`infra/handlers/shared/serving.py` (the `handlers.shared` package already exists) and have
`serve`, the new `submit`, and `worker` handlers import from it. **Decision: factor to shared
module** rather than duplicate, to keep one validation/dependency-construction implementation
(and one place the XSS/validation invariants live). The `serve` handler's public behavior and
tests are unchanged (it re-exports/imports the same functions).

### Decision 4 — Real per-phase progress via optional `on_progress` callback

**Decision: add a keyword-only `on_progress: Callable[[ProgressEvent], None] | None = None` to
`run_agent`, defaulting to `None`. When `None`, control flow and return value are identical to
today.**

New types (new module `src/movieintel/agent/progress.py` so the loop imports them without a
cycle; the serving `jobs.py` re-uses `ProgressPhase`):

```python
class ProgressPhase(StrEnum):
    understanding = "understanding"  # loop start, before first converse
    searching = "searching"  # query_movies / semantic_search dispatched
    comparing = "comparing"  # compare_movies dispatched
    composing = "composing"  # final_answer received (validating/returning)
    refused = "refused"  # guardrail intervention
    bounded = "bounded"  # max_turns reached


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    phase: ProgressPhase
    turn: int  # 1-based turn index at emission (0 at loop start)
    label: str  # human-readable, from PHASE_LABELS[phase]


PHASE_LABELS: dict[ProgressPhase, str] = {
    ProgressPhase.understanding: "understanding your question",
    ProgressPhase.searching: "searching the catalog",
    ProgressPhase.comparing: "comparing titles",
    ProgressPhase.composing: "composing the answer",
    ProgressPhase.refused: "request declined",
    ProgressPhase.bounded: "stopped at the turn limit",
}
```

**Emission points in `run_agent`** — these are line-anchored to the EXACT control flow of the
current `run_agent` (verified against `src/movieintel/agent/loop.py`), not a verbal gloss. Every
emit is wrapped in the `_emit(on_progress, ...)` helper (which is a no-op when `on_progress is
None` and swallows callback exceptions — see control-flow invariants below), so a `None` callback
changes nothing. The loop structure is: a single `understanding` emit before the `for turn in
range(max_turns)` loop; then per turn, in source order, (1) `client.converse(...)`, (2) the
`is_guardrail_intervention(response)` early-return, (3) the `response.get("stopReason") !=
"tool_use"` early-return, (4) append the assistant turn and iterate `_tool_use_blocks(response)`
dispatching each block, (5) the post-loop `return bounded_response(turns_used=max_turns)`.

The emits map to those exact points as follows:

1. **Before the loop**, immediately before `for turn in range(max_turns):` —
   `_emit(..., phase=understanding, turn=0)`. This is the only pre-loop emit.
2. **Guardrail early-return** — immediately before `return safe_refusal()` inside the
   `if is_guardrail_intervention(response):` block — `_emit(..., phase=refused, turn=turn + 1)`.
   (This precedes any assistant-turn append or tool inspection, exactly as the code checks
   guardrail first.)
3. **Non-`tool_use` early-return (bounded mid-loop)** — immediately before
   `return bounded_response(turns_used=turn + 1)` inside the `if response.get("stopReason") !=
   "tool_use":` block — `_emit(..., phase=bounded, turn=turn + 1)`. Note the turn value is
   `turn + 1` here (the current 1-based turn).
4. **Per tool-use block**, inside the `for block in _tool_use_blocks(response):` loop, emitted as
   each block is dispatched, using the block's `name`:
   - `name == "final_answer"` AND `parse_final_answer(tool_input)` returned a valid result (`not
     isinstance(parsed, FinalAnswerError)`): `_emit(..., phase=composing, turn=turn + 1)`
     immediately before `return parsed`.
   - `name == "final_answer"` BUT the payload is invalid (a `FinalAnswerError` is appended as a
     toolResult and the loop `continue`s — it does NOT return): **emit NOTHING for this block.**
     The phase is intentionally left unchanged; it is re-asserted by the next turn's tool
     dispatch (or by a later valid `final_answer`). This is the one branch that keeps looping
     after a `final_answer` attempt, and emitting `composing` here would leave a stale
     `composing` phase while the loop is still running, so no event is emitted.
   - `name in {"query_movies", "semantic_search"}`: `_emit(..., phase=searching, turn=turn + 1)`
     before/at dispatch.
   - `name == "compare_movies"`: `_emit(..., phase=comparing, turn=turn + 1)` before/at dispatch.
   - any other (unknown) tool name: emit NOTHING (the dispatcher returns an `UnknownToolError`
     toolResult and the loop continues; no user-facing phase corresponds to a hallucinated tool).
5. **Post-loop bounded** — immediately before the final `return bounded_response(turns_used=
   max_turns)` after the `for` loop — `_emit(..., phase=bounded, turn=max_turns)`. Note the turn
   value is `max_turns` here (distinct from the mid-loop bounded site in point 3, which uses
   `turn + 1`).

**Tie-break for a turn that mixes tools.** A single turn can dispatch multiple tool blocks (e.g. a
`semantic_search` AND a `compare_movies`). One event is emitted per dispatched block in request
order; the worker's `(phase, turn)`-throttled writer (below) collapses duplicate `(phase, turn)`
writes within a turn, so the **persisted** phase for such a turn is the phase of the LAST dispatched
block in that turn (e.g. a search-then-compare turn persists `comparing` at that turn's `turn + 1`;
the intermediate `searching` at the same `turn` is written first, then overwritten by `comparing`
at the same turn — both share the turn, so the UI's "turn N" stays correct). The `turn` field
(`turn + 1`) lets the UI show "turn 2" even within a phase; the `label` rides along from
`PHASE_LABELS[phase]`.

**Control-flow invariants (owned by `run_agent`):** the callback is invoked for its side effect
only; its return value is ignored; a callback that raises must NOT break the loop —
**Decision: wrap each `on_progress(...)` call in a local `_emit` helper that swallows any
callback exception** (logging at `warning`), because progress reporting is advisory and must
never fail a real agent run. This is the one place the loop tolerates a callback error; it does
not change the result contract.

**Logger provenance (the `_emit`/worker warning+exception logging relies on it).** `loop.py` imports
no `logging` and defines no logger today, nor does the serve handler. Each module that logs under
this design introduces a module-level `logger = logging.getLogger(__name__)` at import: `loop.py`
(for `_emit`), `infra/handlers/worker/handler.py` (progress-write swallow + top-level
`except Exception`), and `src/movieintel/serving/jobs_store.py` (conditional-check swallow). `_emit`
is concretely:

```python
import logging

logger = logging.getLogger(__name__)


def _emit(on_progress, *, phase, turn):
    if on_progress is None:
        return
    try:
        on_progress(ProgressEvent(phase=phase, turn=turn, label=PHASE_LABELS[phase]))
    except Exception:  # noqa: BLE001 - progress is advisory, never fail the run
        logger.warning("on_progress callback raised for phase %s", phase, exc_info=True)
```

The bare `except Exception` carries the `# noqa: BLE001` the ruff `B` ruleset requires (the same
suppression the worker's top-level catch uses). No logging is configured by these modules (Lambda's
runtime attaches the root handler); they only acquire a named logger and emit records.

**Backward compatibility proof obligation (tests):** every existing `tests/agent/test_loop.py`
case calls `run_agent` without `on_progress` and must pass unchanged (AC-7). A new test asserts
the full emission sequence (AC-8) by passing a list-appending callback.

**Worker's progress writer (throttling — write-on-phase-change-OR-turn-change).** The worker
builds `on_progress` as a closure over `JobStore` + `job_id` that calls
`job_store.update_progress(...)` **on the first event OR whenever the incoming event's `(phase,
turn)` pair differs from the last-written `(phase, turn)`**, independent of status: it holds the
last-written `(phase, turn)` (initialized to `None`) and writes when the incoming pair differs (the
very first event always differs from `None`, so it always writes). **Decision: throttle on
`(phase, turn)`, NOT on phase alone.** Writing on phase change ONLY would persist a stale `turn`
across same-phase turns (e.g. two `query_movies` dispatches in `searching` on turns 2 and 3 would
leave `turn: 2`), so the front-end "turn N" would lag the real turn. Keying the throttle on
`(phase, turn)` keeps the user-facing turn current while still bounding writes to at most one per
turn-or-phase transition (≈ one write per turn, a handful per job) regardless of same-turn
duplicate tool blocks. The `turn` and `label` ride along with each write.
`update_progress` writes ONLY `progress`/`updated_at` — never `status` — so the throttle is purely
about progress and has no bearing on the `queued → running` transition.

**Status ownership (`queued → running`), stated once.** The `queued → running` transition is owned
SOLELY by the worker handler, which calls `job_store.mark_running(job_id)` exactly once before
invoking `run_agent` (the handler sketch above shows this). `update_progress` does NOT set
`status` (resolving the earlier ambiguity where both `mark_running` and the first progress write
appeared to flip status). Because `JobStore.create` writes `status=queued` with
`progress.phase=understanding`, and the first `understanding` progress event (turn 0) is emitted
inside `run_agent` AFTER `mark_running` has already set `status=running`, a poll between `create`
and `mark_running` sees `queued`, and any poll after `mark_running` sees `running` — both satisfy
AC-3 (`status` in `{queued, running}`).

**Write-ordering invariant (terminal writes are final).** The `on_progress` callback runs
synchronously INSIDE `run_agent`; every `update_progress` call completes (its `UpdateItem`
returns) before `run_agent` returns control to the worker. The worker then calls
`mark_succeeded`/`mark_failed` AFTER `run_agent` returns. Therefore the natural write order is
always `create` → `mark_running` → zero-or-more `update_progress` → exactly one terminal
(`mark_succeeded` or `mark_failed`), and the terminal write is the LAST write for a job. There is
no async batching of progress writes; `update_progress` is never invoked after a terminal mark.
To make this robust against any future reordering, EVERY mutating method
(`mark_running`/`update_progress`/`mark_succeeded`/`mark_failed`) carries
`ConditionExpression="attribute_exists(PK) AND #s IN (:queued,:running)"` (with `#s` aliasing
`status`), so once a job is terminal (`succeeded`/`failed`) no later write — including a stray
delayed progress write — can overwrite it; a conditional-check failure on a progress write is
caught and swallowed at `warning` level (progress is advisory), while a conditional-check failure
on a terminal write means the job was already terminal and is likewise tolerated (idempotent set).

### IAM (new statements)

`bedrock_invoke_model_resources(region, account)` referenced below is the helper in
**`infra/constants.py`** (the CDK app's module, on `pythonpath`/`mypy_path`), imported into
`infra/serving_stack.py` as `from constants import bedrock_invoke_model_resources` and already used
by the existing `_grant_iam` for `ServingFn`. It is NOT a `movieintel` package symbol. The new
`_grant_async_iam` reuses it exactly as `_grant_iam` does.

**`SubmitFn` role (least privilege — fast path, NO Bedrock, NO GSI/Query/Scan).** Exactly two
`PolicyStatement`s:
- Statement S1 (DynamoDB job keyspace): actions `dynamodb:PutItem`, `dynamodb:UpdateItem`,
  `dynamodb:GetItem`; resource = the base table ARN ONLY
  (`arn:aws:dynamodb:{region}:{account}:table/MovieIntel`). NO GSI1 ARN. NO `Query`/`Scan`/
  `BatchGetItem`. (The submit path only writes/reads a single job item by PK/SK.)
- Statement S2 (worker dispatch): action `lambda:InvokeFunction`; resource = the worker function
  ARN only (`self.worker_function.function_arn`). Implemented via
  `self.worker_function.grant_invoke(self.submit_function)` (CDK scopes this to the function ARN —
  least privilege; acceptable).

**`SubmitFn` explicitly gets NO Bedrock grant (no `InvokeModel`/`ApplyGuardrail`/`Retrieve`), and
NO GSI1/`Query`/`Scan`/`BatchGetItem` grant.** It never runs tools or the agent loop.

**`WorkerFn` role (least privilege — same Bedrock/KB/DynamoDB tool surface as `ServingFn`, plus
job writes).** The GetItem overlap between the tool-read and job statements is intentional and
need NOT be deduped (the union of allows is the behavior; splitting keeps each statement's resource
scope clean). Exactly four `PolicyStatement`s:
- Statement W1 (Bedrock model): action `bedrock:InvokeModel`; resources
  `bedrock_invoke_model_resources(region, account)` (identical to the `ServingFn` grant).
- Statement W2 (guardrail): action `bedrock:ApplyGuardrail`; resource
  `self.guardrail.attr_guardrail_arn` (the in-stack serving guardrail).
- Statement W3 (KB retrieve): action `bedrock:Retrieve`; resource the KB ARN
  `arn:aws:bedrock:{region}:{account}:knowledge-base/YNXUIXEYBQ`.
- Statement W4 (DynamoDB tool reads): actions `dynamodb:GetItem`, `dynamodb:BatchGetItem`,
  `dynamodb:Query`, `dynamodb:Scan`; resources the table ARN AND the GSI1 ARN
  (`.../table/MovieIntel` and `.../table/MovieIntel/index/GSI1`) — identical to the `ServingFn`
  tool grant today.
- Statement W5 (DynamoDB job writes): actions `dynamodb:PutItem`, `dynamodb:UpdateItem`,
  `dynamodb:GetItem`; resource the base table ARN only. (`GetItem` here overlaps W4's `GetItem` —
  acceptable; the two statements have different resource scopes, W4 includes GSI1 and W5 does not.)

No `bedrock:*`, no `Resource: "*"` in any statement. The worker does NOT attach the `_sqlite` layer
(it never touches source DBs). Both new functions attach the existing `SharedPackageLayer`.

**Extended `tests/infra/test_serving_stack_synth.py` assertions (exact).** The synth test (which
templates `MovieIntelServingStack` via `assertions.Template.from_stack`) is extended to assert:
- Two new `AWS::Lambda::Function` resources exist with handlers `handlers.submit.handler.handler`
  and `handlers.worker.handler.handler`; the worker has `Timeout: 300` and `MemorySize: 1024`, the
  submit has `Timeout: 10` and `MemorySize: 256`; neither function's `Layers` includes a `_sqlite`
  layer and both include the shared layer.
- Two new `AWS::ApiGatewayV2::Route` resources with `RouteKey` `POST /jobs` and `GET /jobs/{id}`,
  both integrated to the submit function's integration; the existing `POST /query` and
  `GET /health` routes remain.
- The `SubmitFn` role policy contains the DynamoDB job actions
  (`PutItem`/`UpdateItem`/`GetItem`) scoped to the table ARN with NO GSI1 resource and NO
  `Query`/`Scan`/`BatchGetItem`/`bedrock:*` action, and a `lambda:InvokeFunction` scoped to the
  worker function.
- The `WorkerFn` role policy contains `bedrock:InvokeModel`, `bedrock:ApplyGuardrail`,
  `bedrock:Retrieve`, the DynamoDB tool-read actions on table+GSI1, and the DynamoDB job-write
  actions on the table; NO statement uses `Resource: "*"` and NO statement uses a `bedrock:*`
  wildcard action.
These assertions pin the decomposition so the implementer and the test author cannot disagree.

### CDK changes to `infra/serving_stack.py`

- Add `aws_lambda` worker + submit functions reusing `self.shared_layer` and the
  `INFRA_DIR`/`HANDLER_ASSET_EXCLUDES` asset (the `handlers.worker` and `handlers.submit`
  packages ship automatically under the existing asset root; no exclude change needed).
- `SubmitFn` env: `WORKER_FUNCTION_NAME` (= `self.worker_function.function_name`),
  `MOVIEINTEL_TABLE_NAME`, `MOVIEINTEL_GSI1_NAME` (same table/region env as serving).
- `WorkerFn` env: the full serving env (`_environment()` reused) — model id, guardrail id/version,
  KB id, table/GSI names.
- `WorkerFn` timeout `Duration.minutes(5)` (300s), memory 1024MB (same as `ServingFn`).
  `SubmitFn` timeout `Duration.seconds(10)`, memory 256MB (it only validates + writes + async
  invoke).
- Grant the IAM statements above (`_grant_async_iam()` method; keep `_grant_iam()` for serving
  unchanged).
- Add routes on the existing `self.api`: `POST /jobs` and `GET /jobs/{id}` via a new
  `HttpLambdaIntegration("SubmitIntegration", handler=self.submit_function)`. The `{id}` path
  parameter is a standard HTTP API path variable; the handler reads it from
  `event["pathParameters"]["id"]`, with a defensive fallback that parses the LAST segment of the
  RESOLVED `requestContext.http.path` (`path.rstrip("/").rsplit("/", 1)[-1]`) — never a comparison
  against the `"{id}"` template (see the pinned route-dispatch contract in Decision 1).
- `CfnOutput` for the submit route is informational; the existing `ServingApiUrl` base URL
  output is unchanged (clients append `/jobs`).
- TTL: enable `time_to_live_attribute="expires_at"` on the table **in `pipeline_stack.py`**
  (where the table is defined), per Decision 2.

Env/handler wiring note: `SubmitFn.add_environment("WORKER_FUNCTION_NAME", ...)` must be set
after the worker function is created; grant `lambda:InvokeFunction` on the worker ARN to the
submit role (do not use the broad `worker.grant_invoke` only if it scopes to the function ARN —
it does; `self.worker_function.grant_invoke(self.submit_function)` is acceptable and least
privilege).

### Front end rewrite (`frontend/app.js`)

Rewrite `onSubmit` from single-fetch to submit-then-poll; keep everything else (the
`renderResult`/`render*` DOM builders, `makeBadge`, `sentiment/moodClass`, `extractValidationMessage`,
`safeJson`, `warmUp`, `init` placeholder guard, the aria-live `#status` region) intact. The
`renderNotice(message, variant)` function (textContent-only) is reused verbatim for progress and
errors. The FAKE `STAGES`/`startStages`/`clearStages` timer timeline is **removed** and replaced
by a real poll loop.

New flow:

1. `onSubmit`: validate non-empty text; disable button; `renderNotice("Submitting your
   question...", "progress")`.
2. `POST API_BASE + "/jobs"` with body `{ query: text }` (and `max_turns` only if a future UI
   control sources it from `MAX_TURNS_CAP`; today send only `{query}` — the body schema is
   `extra='forbid'`, same as `/query`).
3. **Submit response dispatch — do NOT reuse `handleResponse`.** `handleResponse(response)`
   branches on `response.ok` FIRST and, when true, calls `renderResult(body)`; a `202` is
   `response.ok === true`, so routing the submit response through `handleResponse` would feed the
   submit body `{job_id, status, poll_url}` into `renderResult`, which switches on `body.kind` and
   falls through to its `default: renderNotice("Unexpected response from the service.", "error")`
   branch. `handleResponse`'s happy path is correct ONLY for the terminal poll body (a 5-kind
   `AgentResult`), never for the submit body. The submit branch therefore handles status itself:
   - `response.status === 202`: `safeJson(response)` → read `job_id`; call `pollJob(job_id)`.
     (This check comes FIRST, before any generic ok/error handling.)
   - `response.status === 400`: `renderNotice(extractValidationMessage(await safeJson(response)),
     "error")`; stop.
   - `response.status === 429`: rate-limit `renderNotice(..., "error")`; stop.
   - `response.status >= 500`: warming-up `renderNotice(..., "error")`; stop.
   - any other status: generic `renderNotice("The service returned an unexpected status (" +
     response.status + ").", "error")`; stop.
   Reuse the individual helpers `extractValidationMessage` and `safeJson` directly; do NOT call
   `handleResponse` for the submit response. **Decision: REMOVE `handleResponse` — it is dead after
   this rewrite.** After the rewrite `onSubmit` no longer hits `/query` and nothing else in `app.js`
   calls `handleResponse`, so it becomes unused; leaving it (still referencing `renderResult`/
   `safeJson`) is avoidable churn. KEEP `renderResult` (used by the poll `succeeded` branch),
   `extractValidationMessage`, `safeJson`, `renderNotice`, every `render*`/`makeBadge`/`sentiment`/
   `moodClass` builder, and `warmUp` (still pings `/health`).
4. **Poll loop** `pollJob(jobId)`: `GET API_BASE + "/jobs/" + encodeURIComponent(jobId)` every
   `POLL_INTERVAL_MS` (1200ms), with a cap `MAX_POLL_ATTEMPTS` (e.g. 150 ≈ 3min, comfortably
   over the 300s worker ceiling is not needed — cap at ~180s of polling) and simple linear
   backoff after a transient poll error (e.g. +500ms, max 3000ms).

   **Branch on HTTP status FIRST** (mirroring the submit dispatch in step 3), THEN on
   `body.status` for a `200`. `404` is `response.ok === false` / `response.status === 404` — it is
   never a `200`, so it must be caught by the status check, NOT by inspecting `body.status`. The
   exact dispatch, in order, for each poll response:
   - `response.status === 404`: terminal — the job is unknown or expired.
     `renderNotice("That request has expired or was not found.", "error")`; `stopPolling()`;
     return. **Terminal — not retried.**
   - `response.status >= 500`: transient — increment attempt, back off, retry until the cap; on
     cap → `renderNotice(...)` error and `stopPolling()`. (Server-side blips are the only
     retryable poll outcome besides a network error.)
   - `!response.ok` (any other non-2xx, e.g. 400/403): terminal — `renderNotice("The service
     returned an unexpected status (" + response.status + ").", "error")`; `stopPolling()`;
     return. **Terminal — not retried.**
   - `response.ok` (a `200`): `safeJson(response)` → read `body`, then switch on `body.status`:
     - `queued` / `running`: `renderNotice(body.progress.label + (body.progress.turn ? " (turn " +
       body.progress.turn + ")" : ""), "progress")` into the aria-live region; schedule the next
       poll (not terminal). **Progress — keep polling.**
     - `succeeded`: `renderResult(body.result)` (the unchanged terminal renderer);
       `stopPolling()`. **Terminal.**
     - `failed`: `renderNotice("The service could not complete your request. Please try again.",
       "error")`; `stopPolling()`. **Terminal.**
     - any other/absent `body.status`: treat as a generic unexpected response —
       `renderNotice("Unexpected response from the service.", "error")`; `stopPolling()`.
       **Terminal.**
   - On a poll fetch that REJECTS (network error, CORS, DNS): increment attempt, back off, retry
     until the cap; on cap → `renderNotice(...)` error and `stopPolling()`. **Retried.**

   To be unambiguous: the ONLY retried outcomes are a `>= 500` response and a rejected fetch
   (network). `404`, any other `!ok` status, `succeeded`, `failed`, and an unknown `body.status`
   are all TERMINAL and call `stopPolling()` immediately (which re-enables the button); they are
   never retried.
5. `finally`-equivalent: a single `stopPolling()` clears the interval/timeout handle; the submit
   button is re-enabled when polling terminates (success/failure/cap) or submit fails. Use a
   module-scoped `pollHandle` (replacing `stageTimer`) cleared in `stopPolling`.

Timers: replace the `stageTimer` array with a single `pollHandle` (setTimeout-driven recursive
poll, not setInterval, so backoff is clean and we never overlap requests). `clearStages` →
`stopPolling`. The button is disabled at submit and re-enabled exactly once when the flow
terminates.

`config.js`: unchanged (`window.API_BASE_URL` is the HTTP API base). The front end simply
targets `/jobs` + `/jobs/{id}` under the same base. No new config value is required.

### Docs

- **`docs/openapi.yaml` (authoritative).** Extend `src/movieintel/serving/openapi.py`'s
  `build_openapi()` to add `POST /jobs` (request `QueryRequest`, 202 `SubmitResponse`, 400
  `ValidationErrorResponse`) and `GET /jobs/{id}` (path param `id`; 200 `JobStatusResponse`
  with the embedded `AgentResult` oneOf; 404 `NotFoundResponse`). Harvest the new component
  schemas (`SubmitResponse`, `JobStatusResponse`, `JobProgress`, `ProgressPhase`, `JobStatus`,
  `WorkerError`, `NotFoundResponse`) via `model_json_schema()` the same way the existing
  generator does, keeping the existing `/query` entry. **`GET /health` is NOT in the current
  OpenAPI and this change does NOT add it — the generator emits `/query`, and now also `/jobs`
  and `/jobs/{id}`. AC-11 is aligned to this (it lists `/query`, `/jobs`, `/jobs/{id}` only, not
  `/health`).** Regenerate the on-disk file with `uv run python -m movieintel.serving.openapi`
  so `test_on_disk_openapi_is_in_sync` passes.
- **`docs/examples/curl.sh`.** **Decision: ADD a new async section** (submit → poll until
  terminal → print the terminal body) while keeping a short synchronous `/query` example for the
  legacy path, so the script demonstrates both and stays runnable (`set -euo pipefail`, `API_URL`
  required, no trailing slash). Document that async is the 503-free path. The job-id parse is
  pinned to `python3 -c` (NOT `grep`/`sed`, which is brittle on JSON and breaks if the submit body
  is ever reformatted); the script already assumes a POSIX environment and `python3` is portable:

  ```bash
  submit=$(curl -sS -X POST "$API_URL/jobs" -H 'content-type: application/json' \
    --data '{"query":"Recommend uplifting adventure movies"}')
  job_id=$(printf '%s' "$submit" | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')

  status=queued
  for _ in $(seq 1 150); do          # max-iteration guard (~180s at 1.2s sleeps)
    body=$(curl -sS "$API_URL/jobs/$job_id")
    status=$(printf '%s' "$body" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')
    [ "$status" = succeeded ] || [ "$status" = failed ] && break
    sleep 1.2
  done
  printf '%s\n' "$body"              # terminal AgentResult (succeeded) or error (failed)
  ```

  The loop stops on either terminal status OR the iteration cap (so it can never spin forever), and
  prints the last fetched body. Keep the standalone synchronous `POST /query` example beneath it.
- **`docs/examples/postman_collection.json`.** Add two requests: `POST {{API_URL}}/jobs` and
  `GET {{API_URL}}/jobs/{{job_id}}`, with a Postman test script on the submit request that saves
  `job_id` into a collection variable, so poll runs immediately after submit.
- **README.** Update section with the Query flow and section 8 (503):
  - Query section: document the async submit-then-poll flow as the recommended path (POST
    `/jobs` → poll `/jobs/{id}`), keep `POST /query` documented as the still-available
    synchronous route, keep `GET /health` and the CloudFront front end at
    `https://d2scp65e5pkne3.cloudfront.net` accurate (the front end now uses the async path).
  - Section 8: change the 503 bullet to state that the async path (202 + poll) **removes** the
    30s-wall 503 by decoupling the loop from the request window; note the synchronous
    `POST /query` remains available and can still hit the 30s wall on heavy queries (legacy
    fallback). Keep the gateway id/base URL references accurate.

### Error handling (per operation)

| Operation | Failure condition | Recoverable? | Caller receives | Logged |
|-----------|-------------------|--------------|-----------------|--------|
| `POST /jobs` body decode/validate | malformed JSON / schema violation | fatal (request) | `400 ValidationErrorResponse` (reused) | no (expected) |
| `POST /jobs` `JobStore.create` | DynamoDB PutItem fails | fatal (request) | `500 {"error":"submit_failed",...}` | yes, `exception` |
| `POST /jobs` worker async-invoke | `lambda.invoke` throttled/denied | recoverable via poll | `202` + job marked `failed` | yes, `error` |
| `GET /jobs/{id}` GetItem | unknown id | n/a | `404 NotFoundResponse` | no |
| `GET /jobs/{id}` GetItem | DynamoDB read fails | transient | `500 {"error":"poll_failed",...}` | yes, `error` |
| Worker `run_agent` | modeled outcome (guardrail/bounded/unknown-tool) | n/a (success) | `succeeded` + `result` (incl. refusal/bounded) | no |
| Worker `run_agent` | unexpected exception (Bedrock/DDB infra) | n/a | `failed` + `WorkerError` (generic msg) | yes, `exception` |
| Worker progress write | DynamoDB UpdateItem fails | advisory | swallowed, run continues | yes, `warning` |
| `on_progress` callback raises | any | advisory | swallowed in `_emit` | yes, `warning` |
| Front-end submit fetch | network/CORS | user-retry | error notice; button re-enabled | console |
| Front-end poll fetch | network/5xx | retry w/ backoff to cap | progress continues; error notice at cap | console |

**Validation rules for new external inputs:**
- `POST /jobs` body: identical to `QueryRequest` — `query` required, type `str`, `min_length=1`;
  `max_turns` optional, type `int`, `1 ≤ max_turns ≤ 12`; no extra fields (`extra='forbid'`).
  Failure → `400 ValidationErrorResponse`.
- `GET /jobs/{id}` path param `id`: required string from `pathParameters.id`; no format
  validation beyond non-empty (treat empty/missing as `404` — a missing param cannot match a
  stored `JOB#` key). This avoids leaking whether an id is "malformed" vs "absent".
- Worker event payload (`{job_id, query, max_turns}`): trusted (constructed by our own submit
  handler, not user-facing); the worker reads `job_id`/`query` directly and treats a missing
  `job_id` as an unrecoverable error (log + return, no record to write).

**Invariant ownership:**
- "A terminal record always exists for every submitted job" — owned by the **worker handler's**
  top-level `try/except` (maps any crash to `failed`) plus the submit handler marking `failed`
  if the async invoke itself fails. The poller therefore always terminates.
- "`run_agent` behaves identically without `on_progress`" — owned by **`run_agent`** via the
  `if on_progress is not None` guards and the `_emit` swallow; proven by the unchanged
  agent-loop test suite.
- "No unvalidated `AgentResult` is ever stored/returned" — owned by **`run_agent`**
  (already validates via `parse_final_answer`) and the `JobStore` round-trip
  (`model_dump`/`model_validate`).
- "Request body strictness (400 on bad input)" — owned by the shared `_parse_request` used by
  both `/query` and `/jobs`.

### Testability

Unit-testable (no AWS, moto for DynamoDB):
- `run_agent` progress emission: pass a list-collecting `on_progress`; assert the event
  sequence for recommendation, comparison, guardrail, and bounded scenarios using the existing
  `CapturingConverseClient`/`converse_*` builders (reuse `tests/serving/conftest.py` fixtures).
- `run_agent` backward compat: existing `tests/agent/test_loop.py` unchanged → passes (AC-7).
- `JobStore` (`tests/serving/test_jobs_store.py`, moto `MovieIntel` table reusing the
  `moto_repository` fixture's table setup): assert
  `create`/`mark_running`/`update_progress`/`mark_succeeded`/`mark_failed`/`get` round-trip and
  TTL `expires_at` presence. **Read-path projection + `Decimal` normalization (locks Fixes 1/3):**
  after `update_progress(JobProgress(phase=searching, turn=2, label=...))`, assert
  `get(job_id).progress.turn == 2` and that it is an `int` (not a `Decimal`), proving `progress` is
  routed through `_from_dynamo`; also assert `get(job_id)` succeeds (does NOT raise) even though the
  stored item carries `PK`/`SK`/`query`/`max_turns`/`expires_at`, proving the explicit projection
  excludes those extras and reconstructs `job_id` from the key. **`AgentResult` alias-as-field proof
  (locks Fix 4):** `mark_succeeded(job_id, RecommendationList(... movies=[RecommendedMovie(pes=90.0,
  ...)]))` then `get(job_id)` and assert `.result == original` (a `RecommendationList` with a `float`
  `pes`), proving the PEP 695 alias validates via smart-union and the `_to_dynamo`/`_from_dynamo`
  `Decimal`/float round-trip is lossless. Also assert the `ConditionExpression` behavior (an
  `update_progress` after a terminal mark is swallowed and does not change `status`) and that `get`
  returns `None` for an unknown id.
- Submit handler (`tests/serving/test_submit_handler.py`, reusing `tests/serving/conftest.py`'s
  `moto_repository`): inject `job_store` + a `FakeLambdaClient` (a small test double defined in
  `tests/serving/conftest.py` that records each `invoke(**kwargs)` call — `FunctionName`,
  `InvocationType`, decoded `Payload` — without invoking anything). Assert the `202` body shape
  (`job_id` is a 32-char hex, `status == "queued"`, `poll_url == f"/jobs/{job_id}"`), that a
  `queued` record is created BEFORE invoke and the `FakeLambdaClient` recorded exactly one
  `InvocationType='Event'` invoke with the matching `job_id`/`query`/`max_turns` payload, `400` on
  a bad body (no record, no invoke), and the async-invoke-failure path (`FakeLambdaClient.invoke`
  raising) marks `failed` + still returns `202`. Also assert the route-dispatch rules (a
  `POST /jobs` event routes to submit; a `GET /jobs/{id}` event in BOTH `routeKey` and resolved-path
  forms routes to poll and extracts the id; a mismatch → `404`) and that `GET /jobs/{id}` returns
  the stored record / `404` / `500 poll_failed` when `JobStore.get` raises.
- Worker handler (`tests/serving/test_worker_handler.py`, reusing `tests/serving/conftest.py`):
  inject `job_store` + scripted Converse client; assert `succeeded` with the right `result`, that
  progress was written on `(phase, turn)` change, and that a deliberately-raising injected client
  yields a `failed` record (never an escaped exception).
- OpenAPI: extend `tests/serving/test_openapi.py` to assert `/jobs` POST + `/jobs/{id}` GET are
  present with 202/400 and 200/404, the new component schemas exist, and the on-disk file is in
  sync.
- CDK synth: extend `tests/infra/test_serving_stack_synth.py` to assert the submit + worker
  functions, the two new routes, and the new IAM statements synthesize.

Integration-testable (manual / `live`-marked, out of CI): the real submit-then-poll round trip
against the deployed API via `curl.sh`.

A design-for-testability note: the submit/worker handlers follow the serve handler's dependency
injection exactly (optional keyword deps defaulting to lazy boto3 builders), so no handler needs
real AWS under pytest. The one hard-to-unit-test piece — the actual async `lambda.invoke` — is
isolated behind an injected `lambda_client`, so tests assert the call arguments without invoking
anything.

### Exact CI commands (from `.github/workflows/ci.yml`, run verbatim)

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

CDK synth (from `infra/`, per `infra/app.py`):

```bash
cd infra && npx aws-cdk@2 synth MovieIntelServingStack
```

Notes for the coder: `mypy` in CI targets `src` only, but `pyproject.toml` sets
`files=["src","tests","infra"]`, so a local `uv run mypy` (no path) type-checks `infra` too —
keep the new infra/handlers code strict-clean. `ruff` selects `E,F,I,UP,B,SIM` at line-length
100. The pre-existing `_sqlite`-dependent failures in `tests/data_access` and
`test_extract_handler` are UNRELATED (gitignored DBs absent) and must not be treated as
regressions. Regenerate OpenAPI with `uv run python -m movieintel.serving.openapi` before
committing so the sync test passes.

### New / modified files summary

New:
- `src/movieintel/agent/progress.py` — `ProgressPhase`, `ProgressEvent`, `PHASE_LABELS`.
- `src/movieintel/serving/jobs.py` — job Pydantic models (`JobStatus`, `JobProgress`,
  `SubmitResponse`, `JobStatusResponse`, `WorkerError`, `NotFoundResponse`).
- `src/movieintel/serving/jobs_store.py` — `JobStore`, `JOB_TTL_SECONDS`.
- `infra/handlers/shared/serving.py` — factored validation + dependency builders.
- `infra/handlers/submit/handler.py` — `POST /jobs` + `GET /jobs/{id}` handler.
- `infra/handlers/worker/handler.py` — async worker running `run_agent`.
- tests: `tests/serving/test_jobs_store.py`, `tests/serving/test_submit_handler.py`,
  `tests/serving/test_worker_handler.py`, `tests/agent/test_loop_progress.py`. The submit/worker
  tests live under `tests/serving/` (consistent with the existing `tests/serving/test_handler.py`)
  and reuse `tests/serving/conftest.py`'s `moto_repository`; a `FakeLambdaClient` recording
  `invoke` kwargs is added to `tests/serving/conftest.py`.

Modified:
- `src/movieintel/agent/loop.py` — add keyword-only `on_progress` + guarded emissions.
- `src/movieintel/persistence/keys.py` — `job_pk`/`job_sk`/`JOB_SK`.
- `src/movieintel/serving/openapi.py` — add `/jobs` + `/jobs/{id}` to `build_openapi`.
- `infra/serving_stack.py` — submit + worker Lambdas, routes, IAM.
- `infra/pipeline_stack.py` — enable `time_to_live_attribute="expires_at"` on the table.
- `infra/handlers/serve/handler.py` — import shared helpers (behavior unchanged).
- `frontend/app.js` — submit-then-poll rewrite.
- `docs/openapi.yaml` (regenerated), `docs/examples/curl.sh`,
  `docs/examples/postman_collection.json`, `README.md`.

---

## Responses to Design Review (latest iteration)

This revision resolves every finding in the current
`.agents/tasks/async-serving/design-review.json` / `design-review.md` (verdict `CHANGES_REQUESTED`:
1 HIGH + 2 MEDIUM + 5 NIT). Each response cites where the design changed. All changes align with the
original requirements and the fixed OPTION C + real-per-phase-progress decisions. The review's
"Verified Assumptions" list (18 source-backed items) confirmed the rest of the design against source;
those sections are unchanged.

- **Finding 1 (HIGH — `JobStore.get` cannot `model_validate` the raw DynamoDB item) — ADDRESSED.**
  Decision 2 now carries a dedicated "`JobStore.get` read-path projection" subsection with the
  concrete implementation. `get` NEVER validates the raw item; it builds an EXPLICIT PROJECTED
  payload of ONLY `JobStatusResponse` model fields — reconstructing `job_id` from the METHOD
  ARGUMENT (the item stores no `job_id`), copying `status`/`progress`/`created_at`/`updated_at`,
  and conditionally adding `result`/`error` — then `model_validate`s THAT payload. The design states
  explicitly that `PK`/`SK`/`query`/`max_turns`/`expires_at` are EXCLUDED before validation (so
  `extra='forbid'` is safe) and that `job_id` is reconstructed from the key. The `JobStatusResponse`
  model note and the lossless-round-trip paragraph were updated to drop the now-wrong "direct
  `model_validate` of the raw item" claim. A `JobStore` round-trip test (Testability, AC-4) asserts
  `get(...)` succeeds despite the stored extras and that `get(...).result` equals a stored
  `RecommendationList` with a `float` `pes`.

- **Finding 2 (MEDIUM — `job_id` form consistency) — ADDRESSED.** A new "`job_id` form (pinned)"
  paragraph in Decision 1 fixes ONE form everywhere: `job_id = uuid4().hex` (lowercase, 32 chars,
  NO dashes); the PK is `job_pk(job_id)` = `JOB#<hex>`; `poll_url = f"/jobs/{job_id}"`. Every dashed
  (`"0f9d2c6e-..."`) example — the `202` body and all three `GET /jobs/{id}` examples — was replaced
  with the hex form (`0f9d2c6e4a1b4c2e9f3d7a6b5c8e1d0f`), and the key-schema PK row now says
  `job_id = uuid4().hex`. The Docs section carries the hex form through curl/postman. No dashed-uuid
  examples remain, so the example, the `job_pk` input, and `poll_url` all agree.

- **Finding 3 (MEDIUM — `Decimal` normalization for `progress`) — ADDRESSED.** The read-path
  projection routes `progress` AND `result` AND `error` through `_from_dynamo` (not just
  `result`/`error`), giving a complete, explicit `Decimal` story rather than resting on an implicit
  Pydantic `Decimal->int` coercion for `progress.turn`. The `JobStore` round-trip test (Testability,
  AC-4) asserts `get(...).progress.turn == 2` and that it is an `int` after
  `update_progress(turn=2)`.

- **Finding 4 (NIT — `AgentResult` PEP 695 alias used as a Pydantic field for the first time) —
  ADDRESSED.** The lossless-round-trip paragraph (Decision 1) and the projection subsection
  (Decision 2) now state that `result: AgentResult | None` validates via Pydantic SMART-UNION on the
  disjoint `kind` discriminator — no `TypeAdapter`, no explicit `Field(discriminator=...)` — and call
  out that this is the first alias-as-field use. The `pes`-float round-trip `JobStore` test is named
  as the explicit alias-as-field proof.

- **Finding 5 (NIT — phase-change-only throttle can persist a stale `turn`) — ADDRESSED.** The
  worker's progress-writer throttle rule is changed to **write on phase change OR turn change**
  (keyed on the `(phase, turn)` pair), so the user-facing "turn N" never lags within a phase while
  writes stay bounded (≈ one per turn-or-phase transition). The tie-break paragraph for mixed-tool
  turns was updated to the `(phase, turn)` throttle wording.

- **Finding 6 (NIT — new handler tests location + `lambda_client` fake unpinned) — ADDRESSED.** The
  files summary and Testability now state the submit/worker tests live under `tests/serving/`
  (consistent with `tests/serving/test_handler.py`), reuse `tests/serving/conftest.py`'s
  `moto_repository`, and use a `FakeLambdaClient` (recording `invoke` kwargs — `FunctionName`,
  `InvocationType`, decoded `Payload`) defined in `tests/serving/conftest.py`.

- **Finding 7 (NIT — poll `500 poll_failed` not in dispatch) — ADDRESSED.** The Decision 1 poll
  branch now states that a `JobStore.get` that RAISES is caught and returned as
  `500 {"error":"poll_failed","message":...}` logged at `error`, matching the error-handling table
  and the front-end `>= 500` retry branch.

- **Finding 8 (NIT — terminal-failure body nests `WorkerError` under `error`) — ADDRESSED.** The
  `JobStatusResponse` model note now states explicitly that a `failed` body intentionally nests the
  `WorkerError` object under the top-level `error` key
  (`{"status":"failed","error":{"error":"worker_failed","message":...}}`) and that the front-end
  `failed` branch and OpenAPI authors must not flatten it.
