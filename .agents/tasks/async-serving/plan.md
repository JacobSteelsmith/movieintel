# Implementation Plan — Async 202 + Poll Serving Path

All work happens inside the worktree `/home/jacob/code/aetna/.worktrees/async-serving` using
ABSOLUTE paths. This plan implements the APPROVED design in
`/home/jacob/code/aetna/.worktrees/async-serving/.agents/tasks/async-serving/design.md`. Read
that design in full before starting; it is the authority on every decision, and nothing here
re-decides architecture.

## Verification commands (discovered from `.github/workflows/ci.yml` and `pyproject.toml`)

Run from the worktree root unless noted. These are the exact CI invocations:

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src          # CI command; local `uv run mypy` (no path) also type-checks tests+infra per pyproject files=
uv run pytest
```

CDK synth (from `infra/`, since `app.py` lives there and there is no global `cdk`):

```bash
cd infra && npx aws-cdk@2 synth MovieIntelServingStack
cd infra && npx aws-cdk@2 synth MovieIntelPipelineStack   # affected by the TTL change
```

Front-end syntax check: `node --check frontend/app.js`.

Regenerate OpenAPI before committing so the sync test passes:
`uv run python -m movieintel.serving.openapi`.

Notes:
- `ruff` selects `E,F,I,UP,B,SIM` at line-length 100. A bare `except Exception` needs
  `# noqa: BLE001` only where the design calls for it (`_emit`, worker top-level, jobs_store
  conditional-check swallow) — but note `BLE001` is NOT in the selected set, so the suppression
  is harmless-but-optional; keep it as the design specifies for consistency with existing code.
- `mypy` is `strict=True` with `files=["src","tests","infra"]`, so new infra/handlers code must
  be strict-clean. `boto3`/`botocore` have `ignore_missing_imports` already.
- PRE-EXISTING failures that are NOT regressions and MUST be ignored: the `_sqlite`-absent
  failures in `tests/data_access/` and `tests/.../test_extract_handler` (the gitignored source
  DBs are absent on base `main`). Do not try to "fix" them.

---

## Ordered steps

- [ ] 1. Add the progress primitives module `src/movieintel/agent/progress.py`.
      Create `ProgressPhase(StrEnum)` with members `understanding`, `searching`, `comparing`,
      `composing`, `refused`, `bounded`; a frozen slotted `@dataclass ProgressEvent(phase,
      turn: int, label: str)`; and `PHASE_LABELS: dict[ProgressPhase, str]` with the six labels
      from design Decision 4. Define a module-level `logger = logging.getLogger(__name__)` here
      (it is imported by `_emit` in `loop.py` — see step 2; either define `_emit` here or in
      `loop.py`, design places it in `loop.py`). Pure stdlib + enum; no AWS.
      Files: `src/movieintel/agent/progress.py`
      Verify: `uv run ruff check src/movieintel/agent/progress.py` and `uv run mypy src` pass.

- [ ] 2. Add the keyword-only `on_progress` callback to `run_agent` with guarded emit points.
      In `src/movieintel/agent/loop.py`: add `import logging` + module `logger`; add a local
      `_emit(on_progress, *, phase, turn)` helper that is a no-op when `on_progress is None` and
      wraps the call in `try/except Exception` logging at `warning` with `# noqa: BLE001` (design
      "Logger provenance" + `_emit` snippet). Add parameter
      `on_progress: Callable[[ProgressEvent], None] | None = None` (keyword-only, after
      `max_turns`) to `run_agent`. Insert the exactly-five emit sites per design Decision 4
      "Emission points": (1) `understanding`/turn 0 before the `for turn in range(max_turns)`
      loop; (2) `refused`/`turn+1` before `return safe_refusal()`; (3) `bounded`/`turn+1` before
      the mid-loop `return bounded_response(turns_used=turn + 1)`; (4) per tool-use block —
      `composing`/`turn+1` before `return parsed` only when valid final_answer, NOTHING for an
      invalid final_answer, `searching`/`turn+1` for `query_movies`/`semantic_search`,
      `comparing`/`turn+1` for `compare_movies`, NOTHING for unknown tools; (5) `bounded`/
      `max_turns` before the post-loop `return bounded_response(turns_used=max_turns)`. When
      `on_progress is None` the control flow and return value MUST be byte-for-byte identical.
      Files: `src/movieintel/agent/loop.py`
      Verify: `uv run pytest tests/agent/test_loop.py` — all existing cases pass UNCHANGED
      (AC-7). Then step 3's new test.

- [ ] 3. Add the progress-emission test `tests/agent/test_loop_progress.py`.
      Using the existing `tests/agent/conftest.py` fixtures (`CapturingConverseClient`,
      `converse_tool_use`/`converse_final`/`converse_guardrail`, `tool_use_block`,
      `make_enriched`, `StubRepository`, `FakeAgentRuntime`, `config`, `kb_config`), pass a
      list-appending `on_progress` callback and assert the emitted `(phase, turn)` sequence for:
      a recommendation flow (understanding/0 → searching/1 → composing/2), a comparison flow
      (search then compare), a guardrail refusal (understanding/0 → refused/1), a mid-loop
      bounded (non-tool_use stop), and a post-loop bounded (max_turns reached → bounded/
      max_turns). Also assert a callback that raises does NOT break the run (result still
      returned). AC-8.
      Files: `tests/agent/test_loop_progress.py`
      Verify: `uv run pytest tests/agent/test_loop_progress.py` passes; `tests/agent/test_loop.py`
      still passes.

- [ ] 4. Add job key helpers to `src/movieintel/persistence/keys.py`.
      Add `JOB_SK = "JOB"`, `def job_pk(job_id: str) -> str: return f"JOB#{job_id}"`, and
      `def job_sk() -> str: return JOB_SK`, alongside the existing `movie_pk`/`meta_sk` (design
      Decision 2 key schema).
      Files: `src/movieintel/persistence/keys.py`
      Verify: `uv run mypy src` and `uv run ruff check src/movieintel/persistence/keys.py` pass.

- [ ] 5. Add the job Pydantic models `src/movieintel/serving/jobs.py`.
      Strict (`extra='forbid'`) models per design Decision 1 "New Pydantic models":
      `JobStatus(StrEnum)` = queued|running|succeeded|failed; reuse `ProgressPhase` from
      `movieintel.agent.progress`; `JobProgress{phase: ProgressPhase, turn: int, label: str}`;
      `SubmitResponse{job_id: str, status: Literal["queued"]="queued", poll_url: str}`;
      `WorkerError{error: Literal["worker_failed"]="worker_failed", message: str}`;
      `NotFoundResponse{error: Literal["not_found"]="not_found", message: str}`;
      `JobStatusResponse{job_id: str, status: JobStatus, progress: JobProgress (REQUIRED),
      created_at: str, updated_at: str, result: AgentResult | None = None,
      error: WorkerError | None = None}` with `model_config = ConfigDict(extra="forbid")`.
      `result` embeds the PEP 695 `AgentResult` alias from `movieintel.agent.response` as a
      field (first such use; smart-union on disjoint `kind`, no TypeAdapter/discriminator).
      Serialize terminal responses with `exclude_none=True`. Do NOT flatten the nested
      `WorkerError` under `error` (design Finding 8).
      Files: `src/movieintel/serving/jobs.py`
      Verify: `uv run mypy src` passes; a quick `uv run python -c "from movieintel.serving.jobs
      import JobStatusResponse; JobStatusResponse.model_json_schema()"` succeeds (alias resolves
      as a field).

- [ ] 6. Add the job store `src/movieintel/serving/jobs_store.py`.
      Implement `JOB_TTL_SECONDS = 86400`, module `logger`, the generic
      `_to_dynamo(model)`/`_from_dynamo(raw)` helpers (JSON round-trip with
      `parse_float=Decimal` / `default=float`) EXACTLY per design Decision 2 (self-contained; do
      NOT reuse repository `_as_number`/`to_item`), and `class JobStore(*, table, config:
      PersistenceConfig)` with `create`, `mark_running`, `update_progress`, `mark_succeeded`,
      `mark_failed`, `get`. `create` writes a COMPLETE initial progress map
      (understanding/turn 0/label) + `expires_at = int(time.time()) + JOB_TTL_SECONDS` with
      `ConditionExpression=attribute_not_exists(PK)`. Every mutating method carries
      `ConditionExpression="attribute_exists(PK) AND #s IN (:queued,:running)"` (alias `#s` →
      `status`); a conditional-check failure on `update_progress` is swallowed at `warning`;
      terminal conditional-check failures are tolerated (idempotent). `get` builds the EXPLICIT
      PROJECTED payload (reconstruct `job_id` from the ARG, copy status/progress/created_at/
      updated_at, conditionally add result/error, pass progress AND result AND error through
      `_from_dynamo`) and returns `JobStatusResponse.model_validate(payload)`; `None` when the
      item is absent. Follows design "`JobStore.get` read-path projection" verbatim.
      Files: `src/movieintel/serving/jobs_store.py`
      Verify: `uv run mypy src` passes; covered by the round-trip test in step 7.

- [ ] 7. Add the job-store test `tests/serving/test_jobs_store.py`.
      Reuse `tests/serving/conftest.py`'s `moto_repository` fixture's table setup (build a
      `JobStore` over the same moto `MovieIntel` table + `persistence_config`). Assert:
      create → get round-trip; `mark_running` flips status to running; `update_progress(
      JobProgress(phase=searching, turn=2, ...))` then `get(job_id).progress.turn == 2` AND
      `isinstance(..., int)` (not Decimal) — locks the `_from_dynamo` projection; `get` succeeds
      despite stored `PK`/`SK`/`query`/`max_turns`/`expires_at` (projection excludes extras,
      reconstructs job_id); `mark_succeeded(job_id, RecommendationList(movies=[RecommendedMovie(
      pes=90.0, ...)]))` then `get(job_id).result == original` (alias-as-field + Decimal/float
      lossless proof); `expires_at` is present on the stored item; an `update_progress` AFTER a
      terminal mark is swallowed and does not change status; `get` returns `None` for an unknown
      id. (AC-4.)
      Files: `tests/serving/test_jobs_store.py`
      Verify: `uv run pytest tests/serving/test_jobs_store.py` passes.

- [ ] 8. Factor shared serving helpers into `infra/handlers/shared/serving.py` and repoint serve.
      Create the `handlers/shared` package (add `infra/handlers/shared/__init__.py` if absent)
      and move the reusable pieces from `infra/handlers/serve/handler.py` into
      `infra/handlers/shared/serving.py`: `_JSON_HEADERS`, `_default_bedrock_client`,
      `_default_kb_client`, `_default_repository`, `_decode_body`, `_bad_request`,
      `_decode_error`, `_parse_request`. Update `infra/handlers/serve/handler.py` to import them
      from `handlers.shared.serving` (keeping its public `handler` behavior and the
      `_default_*`/`_parse_request` names reachable so `tests/serving/test_handler.py`'s
      monkeypatch of `serve_module._default_bedrock_client` etc. still works — re-import the
      names into the serve module namespace). No behavior change.
      Files: `infra/handlers/shared/serving.py`, `infra/handlers/shared/__init__.py` (if
      needed), `infra/handlers/serve/handler.py`
      Verify: `uv run pytest tests/serving/test_handler.py` passes unchanged (AC-10);
      `uv run mypy src` (local `uv run mypy` also checks infra).

- [ ] 9. Add the submit+poll handler `infra/handlers/submit/handler.py`.
      Handler string `handlers.submit.handler.handler`. Dependency-injection signature mirroring
      serve (optional kw deps: `job_store`, `lambda_client`, `config`, `persistence`,
      `kb_config`, defaulting to lazy boto3 builders). Dispatch per design Decision 1
      "Route dispatch contract": SUBMIT on `routeKey == "POST /jobs"` OR (method POST + path
      `/jobs`); POLL on `routeKey == "GET /jobs/{id}"` OR (method GET + path startswith
      `/jobs/`); else `404 NotFoundResponse`. Submit: `_parse_request` (reused → 400
      ValidationErrorResponse on bad body, no record/no invoke), `job_id = uuid4().hex`,
      `JobStore.create(...)` BEFORE invoke, `lambda_client.invoke(FunctionName=env[
      "WORKER_FUNCTION_NAME"], InvocationType="Event", Payload=json({job_id, query, max_turns}))`;
      on invoke failure mark `failed` with WorkerError("could not start worker") and STILL return
      202; on `create` failure return `500 {"error":"submit_failed",...}`. Return `202
      SubmitResponse{job_id, "queued", poll_url=f"/jobs/{job_id}"}`. Poll: read id from
      `event["pathParameters"]["id"]` with fallback `path.rstrip("/").rsplit("/",1)[-1]`,
      empty/missing → 404; `JobStore.get(id)` → None=404, found=200 JobStatusResponse, RAISES →
      `500 {"error":"poll_failed",...}` logged at error. Module `logger`.
      Files: `infra/handlers/submit/handler.py`, `infra/handlers/submit/__init__.py`
      Verify: covered by step 11; `uv run mypy src`.

- [ ] 10. Add the worker handler `infra/handlers/worker/handler.py`.
      Handler string `handlers.worker.handler.handler`. DI signature per design Decision 3
      worker sketch (optional kw deps incl. `job_store`, built lazily via the shared `_default_*`
      builders). Read `job_id`/`query`/`max_turns` from the trusted event; `job_store.mark_running
      (job_id)`; build `on_progress` as the `(phase, turn)`-throttled closure calling
      `job_store.update_progress(JobProgress(...))` (write on first event OR when `(phase, turn)`
      differs from last-written); call `run_agent(query, ..., max_turns=max_turns or MAX_TURNS,
      on_progress=on_progress)`; `job_store.mark_succeeded(job_id, result)`. Wrap in
      `try/except Exception` (the one allowed broad catch, `# noqa: BLE001`) that logs at
      `exception` and calls `job_store.mark_failed(job_id, WorkerError(message="unexpected error
      running the agent"))`. Return `{"job_id": job_id}` (ignored). Module `logger`. A missing
      `job_id` is logged + returned (no record to write).
      Files: `infra/handlers/worker/handler.py`, `infra/handlers/worker/__init__.py`
      Verify: covered by step 12; `uv run mypy src`.

- [ ] 11. Add submit/poll handler tests `tests/serving/test_submit_handler.py` + a `FakeLambdaClient`.
      Add `FakeLambdaClient` to `tests/serving/conftest.py` (records each `invoke(**kwargs)` —
      FunctionName, InvocationType, decoded Payload — without invoking). Build a `JobStore` over
      the `moto_repository` table. Assert per design Testability: submit 202 body shape (`job_id`
      is 32-char hex, `status=="queued"`, `poll_url==f"/jobs/{job_id}"`); a `queued` record exists
      and exactly one `InvocationType='Event'` invoke recorded with matching job_id/query/
      max_turns; 400 on a bad body (no record, no invoke); invoke-failure path (FakeLambdaClient
      raises) marks `failed` but still returns 202; route dispatch (POST /jobs → submit; GET
      /jobs/{id} in BOTH routeKey and resolved-path forms → poll + id extraction; mismatch →
      404); GET poll returns stored record / 404 unknown id / `500 poll_failed` when
      `JobStore.get` raises (inject a store whose `get` raises). AC-1,2,3,4,5,6.
      Files: `tests/serving/test_submit_handler.py`, `tests/serving/conftest.py`
      Verify: `uv run pytest tests/serving/test_submit_handler.py` passes.

- [ ] 12. Add worker handler test `tests/serving/test_worker_handler.py`.
      Reuse `tests/serving/conftest.py` fixtures + a `JobStore` over `moto_repository`; inject a
      scripted `CapturingConverseClient`. Assert: a successful run writes `succeeded` with the
      right `result`; progress is written on `(phase, turn)` change (poll the store or inspect
      written progress); a deliberately-raising injected client yields a `failed` record and NO
      escaped exception (FR-4/AC-9).
      Files: `tests/serving/test_worker_handler.py`
      Verify: `uv run pytest tests/serving/test_worker_handler.py` passes.

- [ ] 13. Extend the OpenAPI generator `src/movieintel/serving/openapi.py` and regenerate.
      In `build_openapi()`, add `POST /jobs` (request `QueryRequest`, 202 `SubmitResponse`, 400
      `ValidationErrorResponse`) and `GET /jobs/{id}` (path param `id`; 200 `JobStatusResponse`
      with the embedded `AgentResult` oneOf; 404 `NotFoundResponse`), keeping the existing
      `/query` entry. Harvest the new component schemas (`SubmitResponse`, `JobStatusResponse`,
      `JobProgress`, `ProgressPhase`, `JobStatus`, `WorkerError`, `NotFoundResponse`) via
      `_collect_schemas`/`model_json_schema()` the same way as today. Do NOT add `/health`.
      Regenerate the on-disk file: `uv run python -m movieintel.serving.openapi`.
      Files: `src/movieintel/serving/openapi.py`, `docs/openapi.yaml` (regenerated)
      Verify: step 14 tests; `test_on_disk_openapi_is_in_sync` passes (AC-11).

- [ ] 14. Extend OpenAPI tests `tests/serving/test_openapi.py`.
      Add assertions that `/jobs` POST (202 + 400) and `/jobs/{id}` GET (200 + 404) are present,
      the new component schemas exist, and the on-disk file stays in sync. Keep the existing
      `/query` assertions.
      Files: `tests/serving/test_openapi.py`
      Verify: `uv run pytest tests/serving/test_openapi.py` passes.

- [ ] 15. Enable DynamoDB TTL on the table in `infra/pipeline_stack.py` + assert it.
      Add `time_to_live_attribute="expires_at"` to the existing `dynamodb.Table(self,
      "MovieIntelTable", ...)` call in `_dynamodb_table()` (design Decision 2). Add one assertion
      to `tests/infra/test_pipeline_stack_synth.py` on the `AWS::DynamoDB::Table`
      `TimeToLiveSpecification` = `{"AttributeName": "expires_at", "Enabled": true}`.
      Files: `infra/pipeline_stack.py`, `tests/infra/test_pipeline_stack_synth.py`
      Verify: `uv run pytest tests/infra/test_pipeline_stack_synth.py` passes;
      `cd infra && npx aws-cdk@2 synth MovieIntelPipelineStack` succeeds.

- [ ] 16. Wire the submit + worker Lambdas, routes, and IAM into `infra/serving_stack.py`.
      Add `self.worker_function` (handler `handlers.worker.handler.handler`, shared layer, full
      `_environment()`, timeout `Duration.minutes(5)`, memory 1024, NO `_sqlite` layer) and
      `self.submit_function` (handler `handlers.submit.handler.handler`, shared layer, timeout
      `Duration.seconds(10)`, memory 256) using the `INFRA_DIR`/`HANDLER_ASSET_EXCLUDES` asset.
      Set submit env `WORKER_FUNCTION_NAME` (after worker is created), plus `MOVIEINTEL_TABLE_NAME`
      /`MOVIEINTEL_GSI1_NAME`. Add `_grant_async_iam()` with the EXACT statements from design
      "IAM": SubmitFn S1 (DynamoDB PutItem/UpdateItem/GetItem on table ARN only, NO GSI1, NO
      Query/Scan) + S2 (`self.worker_function.grant_invoke(self.submit_function)`); WorkerFn W1
      InvokeModel (`bedrock_invoke_model_resources`), W2 ApplyGuardrail (`self.guardrail.
      attr_guardrail_arn`), W3 Retrieve (KB ARN), W4 DynamoDB tool reads on table+GSI1, W5
      DynamoDB job writes on table ARN only. Keep `_grant_iam()` for ServingFn unchanged. Add
      `POST /jobs` and `GET /jobs/{id}` routes on `self.api` via a new
      `HttpLambdaIntegration("SubmitIntegration", handler=self.submit_function)`. Add an
      informational `CfnOutput` for the submit route; keep `ServingApiUrl` unchanged. Call
      `_grant_async_iam()` from `__init__` after the functions exist.
      Files: `infra/serving_stack.py`
      Verify: step 17 synth tests; `cd infra && npx aws-cdk@2 synth MovieIntelServingStack`
      succeeds.

- [ ] 17. Extend serving-stack synth assertions `tests/infra/test_serving_stack_synth.py`.
      Update `_serving_functions` expectations (now THREE python3.12 Lambdas) and the route-count
      assertion (now FOUR routes). Add the design-mandated assertions: two new
      `AWS::Lambda::Function` with handlers `handlers.submit.handler.handler` /
      `handlers.worker.handler.handler`, worker `Timeout:300`/`MemorySize:1024`, submit
      `Timeout:10`/`MemorySize:256`, neither with a `_sqlite` layer and both with the shared
      layer; two new `AWS::ApiGatewayV2::Route` with RouteKey `POST /jobs` and `GET /jobs/{id}`
      integrated to the submit integration, existing `POST /query`/`GET /health` retained; the
      SubmitFn policy has DynamoDB PutItem/UpdateItem/GetItem scoped to the table ARN with NO
      GSI1 and NO Query/Scan/BatchGetItem/`bedrock:*`, plus `lambda:InvokeFunction` scoped to the
      worker; the WorkerFn policy has InvokeModel/ApplyGuardrail/Retrieve, DynamoDB tool reads on
      table+GSI1, DynamoDB job writes on the table, NO `Resource:"*"`, NO `bedrock:*`. Reconcile
      the existing `test_single_serving_lambda_with_handler_string`,
      `test_serving_dynamodb_is_read_only` (now also sees submit/worker write grants — scope the
      read-only assertion to the ServingFn role, or update it per the new reality), and
      `test_api_gateway_integrated_with_the_lambda` route-count expectation so they still pass.
      Files: `tests/infra/test_serving_stack_synth.py`
      Verify: `uv run pytest tests/infra/test_serving_stack_synth.py` passes;
      `cd infra && npx aws-cdk@2 synth MovieIntelServingStack` succeeds (AC-14).

- [ ] 18. Rewrite `frontend/app.js` from single-fetch to submit-then-poll.
      Per design "Front end rewrite": remove the fake `STAGES`/`startStages`/`clearStages` timer
      timeline and the now-dead `handleResponse`; replace `stageTimer` with a single module-scoped
      `pollHandle` cleared by `stopPolling()`. Rewrite `onSubmit` to validate, disable the button,
      `POST API_BASE + "/jobs"` with `{query}`, then dispatch on status FIRST (202 → read job_id →
      `pollJob`; 400 → validation notice; 429; >=500; other). Add `pollJob(jobId)` using recursive
      `setTimeout` at `POLL_INTERVAL_MS = 1200` with a cap (~`MAX_POLL_ATTEMPTS`, ≤180s) and
      linear backoff (+500ms, max 3000ms) on transient errors; branch on HTTP status FIRST (404
      terminal; >=500 retry; other !ok terminal; 200 → switch `body.status`: queued/running →
      progress notice `body.progress.label (+ turn)` and keep polling, succeeded → `renderResult
      (body.result)` + stop, failed → error notice + stop, else → unexpected + stop), and retry on
      a rejected fetch to the cap. Re-enable the button exactly once in `stopPolling`. KEEP all
      `render*`/`makeBadge`/`sentimentClass`/`moodClass`/`renderResult`/`extractValidationMessage`
      /`safeJson`/`renderNotice`/`warmUp`/`init` intact (textContent-only XSS boundary).
      Files: `frontend/app.js`
      Verify: `node --check frontend/app.js` passes (no runtime JS test harness exists; this is
      the syntax gate the task specifies).

- [ ] 19. Update the docs: `curl.sh`, `postman_collection.json`, README.
      `docs/examples/curl.sh`: add an async section (submit → poll loop with the `python3 -c`
      job-id/status parse and a `seq 1 150` iteration cap, print terminal body) per the design
      snippet, keeping a short synchronous `/query` example; note async is the 503-free path;
      keep `set -euo pipefail` and `API_URL` required. `docs/examples/postman_collection.json`:
      add `POST {{API_URL}}/jobs` (with a test script saving `job_id` to a collection variable)
      and `GET {{API_URL}}/jobs/{{job_id}}`. `README.md`: in the Query section document the async
      submit-then-poll flow as recommended (POST `/jobs` → poll `/jobs/{id}`), keep `POST /query`
      documented as the still-available synchronous route and `GET /health` + the CloudFront front
      end accurate; in section 8 change the 503 bullet to state the async path removes the
      30s-wall 503 while synchronous `/query` remains the legacy fallback. Keep the gateway id
      `izizrxgasb` and base URLs accurate. Use hyphens, never em dashes.
      Files: `docs/examples/curl.sh`, `docs/examples/postman_collection.json`, `README.md`
      Verify: `bash -n docs/examples/curl.sh` and `uv run python -c "import json,
      pathlib; json.loads(pathlib.Path('docs/examples/postman_collection.json').read_text())"`
      succeed.

- [ ] 20. Full gate: run the entire CI command set and fix anything new.
      Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`,
      `uv run pytest`, `cd infra && npx aws-cdk@2 synth MovieIntelServingStack`,
      `cd infra && npx aws-cdk@2 synth MovieIntelPipelineStack`, `node --check frontend/app.js`.
      Confirm the ONLY failing tests are the pre-existing `_sqlite`-absent cases in
      `tests/data_access/` and `test_extract_handler` (unchanged from base `main`).
      Files: (none — verification only; apply fixes where failures surface)
      Verify: all commands succeed except the documented pre-existing `_sqlite` failures.
