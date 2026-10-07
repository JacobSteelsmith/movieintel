# Prompt-Engineering Examples

This document holds the varied prompt/response examples that the top-level
[`README.md`](../../README.md) §7 summarizes. It covers the two prompting surfaces in
the system: the **enrichment** structured-output prompt (Subsystem A) and the **agent**
tool-use + `final_answer` prompting (Subsystem B).

> **Illustrative / representative, not verified records.** Every movie fact and every
> JSON body below is **illustrative** — fabricated to show shape, not drawn from a
> verified enrichment run. Any `/query` body shown is **representative** of the real
> request/response contract, not a captured live response. The field names and enum
> values, however, match the real Pydantic schemas exactly
> ([`domain/schemas.py`](../../src/movieintel/domain/schemas.py),
> [`agent/response.py`](../../src/movieintel/agent/response.py),
> [`serving/schemas.py`](../../src/movieintel/serving/schemas.py)).

---

## 1. Enrichment prompting (Subsystem A)

Source: [`enrichment/prompts.py`](../../src/movieintel/enrichment/prompts.py),
[`enrichment/client.py`](../../src/movieintel/enrichment/client.py).

**Strategy (design §7.4):** schema-constrained Converse JSON, Pydantic validation, then a
bounded repair turn. The model is asked for **exactly one JSON object and nothing else**.

- **System prompt** pins the role (film-industry analyst), the strict output contract
  (one JSON object, no prose, no markdown fences), the enum-only constraint, the
  `"unknown"` sentinel for a missing budget/revenue tier (never fabricate), and the
  invariant that the **deterministic PES value is given and must not be altered**.
- **User message** embeds the collected movie facts, states the deterministic
  `deterministic_pes_value`, and appends the `EnrichmentAttributes` JSON Schema
  (`model_json_schema()`), asking the model to assign `effectiveness.tier` /
  `effectiveness.explanation` *from* that value and echo the value back verbatim.
- **Bounded repair:** on a JSON-parse or Pydantic-validation failure, a repair turn echoes
  the validation error and asks for a corrected single JSON object. Repair is bounded by
  `MAX_REPAIR_ATTEMPTS` (default `2`,
  [`enrichment/client.py`](../../src/movieintel/enrichment/client.py)); on exhaustion the
  movie is recorded as a per-item `Failed` outcome and the batch continues (never aborts).
- **Outcomes** ([`enrichment/results.py`](../../src/movieintel/enrichment/results.py)):
  `Ok | Repaired | Failed | Blocked`.

### Example 1a — valid first-pass enrichment (illustrative)

**Illustrative facts given to the model:** a mid-budget, well-reviewed sci-fi drama with a
deterministic PES value of `78.42` (computed in code, passed in).

Representative single JSON object the model returns, matching `EnrichmentAttributes`:

```json
{
  "overview_sentiment": "positive",
  "budget_tier": "medium",
  "revenue_tier": "high",
  "effectiveness": {
    "value": 78.42,
    "roi_defined": true,
    "rating_defined": true,
    "tier": "standout",
    "explanation": "Strong revenue multiple on a moderate budget plus favorable audience ratings place this well above the solid band."
  },
  "mood": "intense",
  "reasoning_basis": "Revenue far exceeds budget (high revenue tier, medium budget tier); the given PES value and positive reception support a standout tier."
}
```

### Example 1b — missing-budget safe handling (illustrative)

**Illustrative facts:** a film with a `NULL`/zero budget. PES falls back to the rating-only
branch, so the given `deterministic_pes_value` is `55.00`, `roi_defined` is `false`, and the
budget tier is the explicit sentinel.

```json
{
  "overview_sentiment": "neutral",
  "budget_tier": "unknown",
  "revenue_tier": "medium",
  "effectiveness": {
    "value": 55.0,
    "roi_defined": false,
    "rating_defined": true,
    "tier": "solid",
    "explanation": "Budget is unavailable, so effectiveness rests on audience rating alone; a mid-range rating yields a solid tier."
  },
  "mood": "lighthearted",
  "reasoning_basis": "No budget figure is available, so budget_tier is 'unknown' rather than guessed; revenue is mid-range."
}
```

### Example 1c — malformed-then-repaired (illustrative)

First attempt returns an out-of-set `mood` (`"melancholy"`), which Pydantic rejects. The
repair turn echoes the validation error; the second attempt corrects `mood` to an allowed
member (e.g. `"dark"`) and the result is recorded as `Repaired(attempts=1)`.

---

## 2. Agent prompting (Subsystem B)

Source: [`agent/loop.py`](../../src/movieintel/agent/loop.py),
[`agent/response.py`](../../src/movieintel/agent/response.py),
[`agent/tools/specs.py`](../../src/movieintel/agent/tools/specs.py).

**Strategy (design §3.7):** an explicit Converse loop. Each turn attaches the three tool
specs (`query_movies`, `semantic_search`, `compare_movies`) plus a dedicated `final_answer`
tool and the `guardrailConfig`. While `stopReason == "tool_use"`, the loop validates the
tool args, dispatches the tool, and returns a `toolResult`. The model ends the turn by
calling `final_answer` with one of three `kind`s. The loop validates that payload with
Pydantic ([`parse_final_answer`](../../src/movieintel/agent/response.py)) into a terminal
`AgentResult`. A Guardrail intervention returns a `RefusalResponse` (kind `refusal`); the
`MAX_TURNS` bound (default `6`) returns a `BoundedResponse` (kind `bounded`).

The three model-selected terminal shapes mirror `agent/response.py` exactly.

### Example 2a — recommendations (illustrative)

**Input:** `Recommend action movies with high revenue and positive sentiment.`

Representative tool-use trace: `query_movies(sentiment="positive", min_revenue=..., genres=["Action"], sort_by="pes")` →
`final_answer(kind="recommendations", ...)`. Representative `AgentResult`
(`RecommendationList`):

```json
{
  "kind": "recommendations",
  "query_summary": "High-revenue action movies with positive overview sentiment, ranked by Production Effectiveness Score.",
  "movies": [
    {
      "movie_id": "603",
      "title": "Illustrative Action Film A",
      "sentiment": "positive",
      "pes": 88.10,
      "mood": "intense",
      "rationale": "Top revenue in the action set with a positive overview and a standout PES."
    },
    {
      "movie_id": "411",
      "title": "Illustrative Action Film B",
      "sentiment": "positive",
      "pes": 81.55,
      "mood": "tense",
      "rationale": "Strong revenue multiple and favorable sentiment; just below the leader on PES."
    }
  ]
}
```

### Example 2b — preferences (illustrative)

**Input:** `Summarize preferences for a user based on their ratings.`

Representative `AgentResult` (`PreferenceSummary`):

```json
{
  "kind": "preferences",
  "subject": "user ratings profile",
  "summary": "This viewer favors high-effectiveness action and sci-fi titles with positive or intense moods, and rates slower dramas lower.",
  "highlights": [
    "Leans toward intense, high-PES action films.",
    "Positive sentiment correlates with the viewer's top ratings.",
    "Lower ratings cluster on low-revenue, lighthearted titles."
  ]
}
```

### Example 2c — comparison (illustrative)

**Input:** `Compare the two highest-PES science-fiction films.`

Representative tool-use trace: `query_movies` (to find the two highest-PES sci-fi titles) →
`compare_movies(movie_ids=["1726", "157336"], dimensions=["revenue", "pes", "runtime"])` →
`final_answer(kind="comparison", ...)`. Representative `AgentResult`
(`ComparativeAnalysis`):

```json
{
  "kind": "comparison",
  "subjects": ["Illustrative Sci-Fi Film A", "Illustrative Sci-Fi Film B"],
  "dimensions": ["revenue", "pes", "runtime"],
  "narrative": "Film A edges Film B on PES on the strength of a higher revenue multiple, while Film B is the longer runtime; both land in the standout tier."
}
```

---

## 3. Representative POST /query pair

Source: [`serving/schemas.py`](../../src/movieintel/serving/schemas.py),
[`docs/openapi.yaml`](../openapi.yaml), [`docs/examples/curl.sh`](curl.sh).

**Representative** request body (`QueryRequest`: required non-empty `query`, optional
`max_turns` in `[1, 12]`):

```json
{ "query": "Recommend action movies with high revenue and positive sentiment." }
```

The success response body (HTTP 200) is the serialized `AgentResult` exactly as the loop
returns it (see Example 2a). A schema-violating body (for example, missing `query`) returns
HTTP 400 with a `ValidationErrorResponse`:

```json
{
  "error": "invalid_request",
  "detail": [
    { "loc": ["query"], "msg": "Field required", "type": "missing" }
  ]
}
```
