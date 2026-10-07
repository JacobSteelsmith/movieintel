# Responsible AI: Guardrails and Least-Privilege IAM

This document records the Responsible-AI posture of MovieIntel (REQ-X-4): the finalized
Amazon Bedrock Guardrail policy, how it is enforced on every model call, and a summary of
the least-privilege IAM audit (REQ-X-5.4). It describes behavior as implemented and
synthesized; applying any Guardrail or IAM change to live resources requires a redeploy.

## Guardrail policy

Two Guardrails are deployed, split by threat model:

- `movieintel-enrichment-guardrail`, defined in
  [`infra/pipeline_stack.py`](../infra/pipeline_stack.py) (`_guardrail`), applied by the
  Enrich Lambda on the enrichment pipeline (Subsystem A).
- `movieintel-serving-guardrail`, defined in
  [`infra/serving_stack.py`](../infra/serving_stack.py) (`_guardrail`), applied by the
  serving Lambda on the agent loop (Subsystem B).

The two Guardrails share the SAME harmful-content filters, denied topic, and PII policy.
That shared policy is factored into reusable helpers in
[`infra/constants.py`](../infra/constants.py) (`guardrail_content_filters`,
`guardrail_denied_topics`, `guardrail_pii_entities`), so the two Guardrails are defined
once and differ ONLY by `PROMPT_ATTACK`. The policy is deliberately conservative and
movie-domain focused.

### Why two Guardrails: right control for the right threat surface

Enrichment and serving face different threats, so they get different `PROMPT_ATTACK`
treatment. This is a Responsible-AI improvement, not a weakening: the control that
mattered is kept exactly where untrusted input enters the system.

- Enrichment is a trusted batch path. Its input is the system's OWN enrichment prompt
  built from database rows, with no untrusted end-user text. A `PROMPT_ATTACK` input
  filter on this path was misclassifying the enrichment prompt's strict JSON-formatting
  directives as prompt injection and blocking the request on input: in a clean-slate
  top-100 run, roughly 98 of 100 enrichments were blocked. The enrichment Guardrail
  therefore OMITS `PROMPT_ATTACK`.
- Serving is interactive. The `/query` route takes untrusted free-text from end users,
  which is exactly the surface prompt injection targets. The serving Guardrail KEEPS
  `PROMPT_ATTACK` on input (strength `HIGH`; Bedrock applies it to the input turn only, so
  output is `NONE`).

### Content filters

The five harmful-content filters are enabled on BOTH Guardrails at `HIGH` strength on both
Converse input and output. `PROMPT_ATTACK` is present only on the serving Guardrail
(input `HIGH`, output `NONE`).

| Filter | Input | Output | Guardrail | Why it fits a movie assistant |
| --- | --- | --- | --- | --- |
| HATE | HIGH | HIGH | both | Overviews and user queries can touch sensitive themes; block hateful content either direction. |
| VIOLENCE | HIGH | HIGH | both | Film plots are violent, but the model's own output should not generate gratuitous violent content. |
| SEXUAL | HIGH | HIGH | both | Keeps summaries and recommendations free of explicit sexual content. |
| INSULTS | HIGH | HIGH | both | Prevents demeaning content in generated recommendations and refusals. |
| MISCONDUCT | HIGH | HIGH | both | Blocks requests to facilitate wrongdoing dressed up as a movie question. |
| PROMPT_ATTACK | HIGH | NONE | serving only | Hardens the agent loop against prompt injection carried in untrusted user `/query` text. Omitted on enrichment, a trusted batch path with no user input, where it was misclassifying our own JSON-formatting instructions and blocking ~98/100 enrichments. |

### Denied topics

One denied topic, `non-movie-advice`, blocks medical, legal, or financial advice unrelated
to movie-metadata enrichment (for example, stock picks or symptom diagnosis). The scope is
intentionally narrow so legitimate movie questions are never refused.

### PII handling

PII entities `EMAIL` and `PHONE` are set to `ANONYMIZE` (not `BLOCK`). These are genuinely
incidental contact details that have no place in movie enrichment output, so anonymizing
them is safe and non-disruptive.

`NAME` is intentionally NOT in the PII set. A movie-domain assistant must be able to emit
real people's names: directors, actors, and the characters they play are core enrichment
content. Applying `ANONYMIZE` to `NAME` rewrites those legitimate names in the model's
structured JSON output, which corrupts the JSON and causes the enrichment to be treated as
a guardrail intervention. This was observed directly: with `NAME` on `ANONYMIZE`, 98 of 100
enrichments in a clean-slate top-100 run were blocked on output (0 failed on repair), and
the guardrail trace showed the `NAME` policy anonymizing names such as cast and crew. For
this domain, anonymizing names is a misapplication of PII protection, not real privacy
protection, so `NAME` is excluded. `EMAIL`/`PHONE` remain anonymized as incidental contact
details.

### Hyphens, not em dashes

All Guardrail names, descriptions, topic definitions, examples, and blocked-messaging
strings use hyphens, never em dashes, per AWS steering. A synth assertion enforces this.

## Fail-closed enforcement on every Converse call

A Guardrail is attached to every single Converse call in both subsystems: enrichment
requests apply the enrichment Guardrail, serving turns apply the serving Guardrail. This is
a fail-closed invariant: [`BedrockConfig.guardrail_config`](../src/movieintel/config.py)
raises `GuardrailNotConfiguredError` when no Guardrail id is configured, so a missing
Guardrail id stops the call rather than silently sending an unguarded request. Each Lambda
receives its own `BEDROCK_GUARDRAIL_ID` from its stack (the Enrich Lambda points at the
enrichment Guardrail, the serving Lambda at the serving Guardrail).

- Enrichment: `enrichment/client.py` attaches `guardrailConfig` on every request.
- Serving agent loop: `agent/loop.py` attaches `guardrailConfig` on every turn.

Enforcement is proven by existing tests:

- `tests/enrichment/test_enrich_movie.py` asserts the Guardrail config is attached on every
  enrichment request, and that a blocked response yields a `Blocked` outcome with no
  content.
- `tests/agent/test_loop.py` (section 8) asserts `guardrailConfig` is present on every
  agent-loop Converse call.
- `tests/serving/test_handler.py::test_guardrail_intervention_returns_refusal_at_200`
  asserts a serving-side intervention returns a safe `RefusalResponse` at HTTP 200.
- `tests/enrichment/test_config.py::test_guardrail_config_raises_when_id_unset` asserts the
  fail-closed behavior.

### Intervention handling

- Enrichment: a Guardrail intervention is recorded as `Blocked` for that item and the item
  is skipped as a tolerated per-item outcome; the batch continues.
- Serving: a Guardrail intervention produces a safe refusal response to the caller (HTTP
  200 with a refusal body), never a leaked or partial unsafe answer.

### Guardrail-coverage metric

The eval harness computes a `guardrail_coverage` metric
([`src/movieintel/eval/metrics.py`](../src/movieintel/eval/metrics.py)) that must be 1.0.
Every evaluated record is marked guardrail-attached because every Converse call attaches a
Guardrail. This is asserted in `tests/eval/test_harness.py`.

## Least-privilege IAM audit (REQ-X-5.4)

The IAM across all three stacks was audited. No stack grants `bedrock:*`,
`s3vectors:*`, or Resource `*` for data or model access. Model access is scoped via
`infra/constants.py` (`bedrock_invoke_model_resources` plus the inference-profile
constants), with no inline resource or model literals.

Two over-broad grants were found and tightened in
[`infra/pipeline_stack.py`](../infra/pipeline_stack.py) (`_grant_iam`):

1. The `persist` Lambda previously held `grant_read_write_data` on the DynamoDB table, but
   its handler only performs an idempotent `PutItem`. It is now `grant_write_data`
   (write-only).
2. The `embed_vector` Lambda previously held `grant_read_write_data` on the DynamoDB table,
   but its handler never touches DynamoDB (it only embeds overviews and upserts vectors).
   That grant was removed entirely.

Everything else was already least-privilege and is left unchanged:

- Enrichment `bedrock:InvokeModel` is scoped to exactly the Sonnet 4.5 inference-profile
  ARN plus the three cross-region foundation-model ARNs, and `bedrock:ApplyGuardrail` to
  the enrichment Guardrail ARN.
- `embed_vector` `bedrock:InvokeModel` is scoped to the Titan v2 foundation-model ARN, and
  `s3vectors:PutVectors` to the index ARN.
- Serving `bedrock:ApplyGuardrail` is scoped to the in-stack serving Guardrail ARN (an
  `Fn::GetAtt` token, no longer a pinned literal), and DynamoDB access is read-only
  (`GetItem`, `BatchGetItem`, `Query`, `Scan`) scoped to the table and GSI1.
- The Knowledge Base service role is scoped to the Titan v2 model ARN and the S3 Vectors
  index ARN, with a confused-deputy-guarded trust policy.

These properties are pinned by synth assertions in `tests/infra/` (pipeline, serving, and
knowledge-base synth tests), including exact-ARN-list checks on the InvokeModel grants and
a regression guard that the `embed_vector` role carries no DynamoDB action.

> Note: these changes are synthesized and committed but NOT deployed. Both
> `MovieIntelPipelineStack` and `MovieIntelServingStack` must be redeployed for the
> two-Guardrail split to take effect: the pipeline redeploy updates the existing
> `movieintel-enrichment-guardrail` in place (removing `PROMPT_ATTACK`), and the serving
> redeploy creates the new `movieintel-serving-guardrail` and repoints the serving Lambda
> env and IAM at it.
