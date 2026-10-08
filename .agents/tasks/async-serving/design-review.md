# Design Review — Async 202 + Poll Serving Path (re-review)

Reviewed: `/home/jacob/code/aetna/.worktrees/async-serving/.agents/tasks/async-serving/design.md`
Scope: confirm the prior review's 1 HIGH + 2 MEDIUM + 5 NIT findings are resolved and that no new
blocking gaps were introduced. OPTION C (keep API Gateway v2 HTTP API, dedicated worker Lambda,
on_progress callback) and real-per-phase-progress are FIXED and not relitigated. Source re-checked
where the fixes depend on it.

Verdict: APPROVED (0 HIGH, 0 MEDIUM; remaining items are NITs only).

---

## Prior findings — resolution status

### 1. HIGH (prior) — `JobStore.get` cannot `model_validate` the raw DynamoDB item — RESOLVED

Decision 2 now carries a dedicated "`JobStore.get` read-path projection (REQUIRED)" subsection with
a concrete implementation. `get` NEVER validates the raw item: it builds an EXPLICIT PROJECTED
payload containing only `JobStatusResponse` model fields, reconstructs `job_id` from the method
argument (the item stores none), copies `status`/`progress`/`created_at`/`updated_at`, and
conditionally adds `result`/`error`, then `model_validate`s THAT payload. The design states
explicitly that `PK`/`SK`/`query`/`max_turns`/`expires_at` are EXCLUDED before validation (so
`extra='forbid'` is safe) and that `job_id` is reconstructed from the key. The `JobStatusResponse`
model note and the lossless-round-trip paragraph were updated to drop the old "direct
`model_validate` of the raw item" claim. AC-4 and the `JobStore` round-trip test lock the behavior.
Fully meets the mandated fix.

### 2. MEDIUM (prior) — `job_id` form inconsistency — RESOLVED

A pinned "`job_id` form (used everywhere)" paragraph in Decision 1 fixes ONE form: `job_id =
uuid4().hex` (32-char lowercase, no dashes); PK = `job_pk(job_id)` = `JOB#<hex>`; `poll_url =
f"/jobs/{job_id}"`. All three former dashed-uuid JSON examples (the `202` body and the three
`GET /jobs/{id}` examples) are now the hex form `0f9d2c6e4a1b4c2e9f3d7a6b5c8e1d0f`; the key-schema PK
row says `job_id = uuid4().hex`; the Docs/curl/postman carry the hex form through. No dashed-uuid
examples remain. Fully meets the mandated fix.

### 3. MEDIUM (prior) — `progress.turn` Decimal normalization — RESOLVED

The read-path projection routes `progress` AND `result` AND `error` through `_from_dynamo` (not just
`result`/`error`), giving an explicit, complete Decimal story rather than resting on implicit Pydantic
`Decimal->int` coercion. The `JobStore` round-trip test (Testability, AC-4) asserts
`get(...).progress.turn == 2` and that it is an `int` after `update_progress(turn=2)`, AND that a
`RecommendationList` with a `float` `pes` round-trips (`get(...).result == original`). Fully meets the
mandated fix.

### 4. NIT (prior) — `AgentResult` PEP 695 alias as a Pydantic field — RESOLVED

Decision 1's lossless-round-trip paragraph and Decision 2's projection subsection now state that
`result: AgentResult | None` validates via Pydantic SMART-UNION on the disjoint `kind` discriminator
— no `TypeAdapter`, no explicit `Field(discriminator=...)` — flag it as the first alias-as-field use,
and name the `pes`-float round-trip test as the explicit proof. Verified against source: `AgentResult`
is a 5-member `type` alias of disjoint-`kind`, `extra='forbid'` models and `RecommendedMovie.pes` is a
`float`.

### 5. NIT (prior) — phase-change-only throttle persists stale `turn` — RESOLVED

The worker progress-writer rule is now "write on phase change OR turn change" (keyed on the
`(phase, turn)` pair, initialized to `None`). The tie-break paragraph for mixed-tool turns was updated
to the `(phase, turn)` wording. Writes stay bounded (approximately one per turn-or-phase transition)
while the user-facing turn no longer lags. A chosen throttle rule is specified.

### 6. NIT (prior) — handler test placement + unnamed `lambda_client` fake — RESOLVED

Testability and the files summary state the submit/worker tests live under `tests/serving/`
(consistent with `tests/serving/test_handler.py`), reuse `tests/serving/conftest.py`'s
`moto_repository`, and use a `FakeLambdaClient` (records `invoke` kwargs — `FunctionName`,
`InvocationType`, decoded `Payload`) defined in `tests/serving/conftest.py`.

### 7. NIT (prior) — poll `500 poll_failed` missing from dispatch — RESOLVED

The Decision 1 poll branch now states a raising `JobStore.get` is caught and returned as
`500 {"error":"poll_failed","message":...}` logged at `error`, matching the error-handling table and
the front-end `>= 500` retry branch.

### 8. NIT (prior) — nested `WorkerError` terminal-failure body shape — RESOLVED

The `JobStatusResponse` note states explicitly that a `failed` body intentionally nests the
`WorkerError` under the top-level `error` key
(`{"status":"failed","error":{"error":"worker_failed","message":...}}`) and that the front-end
`failed` branch and OpenAPI authors must not flatten it.

---

## New findings (this iteration)

None at HIGH or MEDIUM. The revised sections are internally consistent: the pinned hex `job_id`,
the key schema, `poll_url`, the examples, the projection, and the throttle rule all agree, and the
testability section's assertions line up with the mandated fixes (projection excludes extras,
`progress.turn == 2` int, `pes` float round-trip, `FakeLambdaClient` invoke assertions, poll
`404`/`500 poll_failed`).

### N1. NIT — curl.sh poll loop uses `&&` as a conditional break without an explicit `if`

Where: Docs section, `curl.sh` snippet: `[ "$status" = succeeded ] || [ "$status" = failed ] && break`.

Problem: With `set -euo pipefail`, this `[ ... ] || [ ... ] && break` chain is correct for breaking
(the `&&` binds to the whole `||` result in POSIX left-to-right evaluation), but when neither branch
matches, the compound command's exit status is non-zero, which under `set -e` could terminate the
script were it not the last command before `sleep`. It happens to be safe here because `sleep` follows
on the next line and resets `$?`, but it is fragile. This is a documentation example, not product code,
so it does not block.

Concrete fix: Use an explicit guard: `if [ "$status" = succeeded ] || [ "$status" = failed ]; then
break; fi`.

---

## Verified Assumptions (checked against source this iteration)

1. `AgentResult` is a PEP 695 `type` alias
   (`type AgentResult = RecommendationList | PreferenceSummary | ComparativeAnalysis |
   BoundedResponse | RefusalResponse`), all five `extra='forbid'` with disjoint `kind` literals —
   confirms the smart-union-as-field claim (Finding 4 fix). VERIFIED in
   `src/movieintel/agent/response.py`.
2. `RecommendedMovie.pes` is a `float` — confirms the Decimal/float round-trip need (Finding 3 fix).
   VERIFIED in `src/movieintel/agent/response.py`.
3. `run_agent` current signature has NO `on_progress` parameter and the module imports no `logging`
   — consistent with the design's "keyword-only addition, default None, byte-for-byte identical when
   None" and "introduce a module logger" claims (FR-5, logger provenance). VERIFIED in
   `src/movieintel/agent/loop.py`.
4. `run_agent` control flow matches the design's emission points exactly: single pre-loop entry;
   guardrail checked first then `return safe_refusal()`; `stopReason != "tool_use"` →
   `return bounded_response(turns_used=turn + 1)`; per-block dispatch over `_tool_use_blocks`;
   valid `final_answer` → `return parsed`; invalid `final_answer` → append error toolResult +
   `continue` (no return); post-loop `return bounded_response(turns_used=max_turns)` (distinct
   `turn+1` vs `max_turns` sites). VERIFIED in `src/movieintel/agent/loop.py`.
5. `src/movieintel/serving/jobs.py` and `src/movieintel/serving/jobs_store.py` do not yet exist, so
   the models and `JobStore` are design-only; nothing in source contradicts the projection,
   `_to_dynamo`/`_from_dynamo`, or key-schema design. VERIFIED (no files found).

(The prior review's 18-item source-backed Verified Assumptions list — loop flow, QueryRequest
strictness, shared-handler factorability, keys module, repository float handling, IAM resources,
CORS/WAF, TTL construct, OpenAPI generator, front-end app.js, CI commands, fixtures, README state —
remains valid and is not re-litigated here.)

## Unverified / Wrong Assumptions

None. The two previously WRONG/INCONSISTENT items (Finding 1 raw `model_validate`; Finding 2
hex-vs-dashed `job_id`) are now corrected in the design, and the two previously UNDER-SPECIFIED/
UNVERIFIED items (Finding 3 `progress` Decimal; Finding 4 alias-as-field) are now explicitly specified
and backed by named tests. No remaining claim rests on an unverified assumption.
