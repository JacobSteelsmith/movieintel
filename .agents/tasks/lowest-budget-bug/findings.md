# Findings: "lowest budget" comparison returns the highest-budget movies

Read-only investigation of `/home/jacob/code/aetna` on branch `main` (the deployed code). No code was changed. The `.worktrees/async-serving` and `fix/serving-max-turns-clamp` branches were explicitly not consulted.

## Summary answer

The agent returned the two **highest**-budget movies for a "lowest budget" request because **the `query_movies` tool cannot express "lowest budget."** Its sort is hard-coded to **descending** direction with no sort-direction parameter, and its only budget controls are `min_budget` / `max_budget` **range filters**, not an ordering. There is no faithful tool path by which the model could have obtained the lowest-budget pair. Faced with an unexpressible request, the model improvised: it got a budget-ranked list that is highest-first (or defaulted to the PES ranking), picked the top ids, and fed those ids to `compare_movies`. `compare_movies` is purely deterministic given ids, so the wrong movies came from the model's **selection upstream**, not from the comparison itself. The system prompt compounds the problem by framing `query_movies` as "ranked by Production Effectiveness Score (PES)" and never mentioning ascending/lowest ordering.

This is an **agent/tool-logic bug** (a missing capability in the tool contract), not a data problem.

## Evidence (file / symbol citations — all verified in code)

### 1. `query_movies` sorts descending only; there is no ascending/direction option

`src/movieintel/agent/tools/query_movies.py`, `_sort_records()`:

```python
def _sort_records(records, sort_by, used_index):
    if sort_by == "pes" and used_index:
        return records
    # None sorts last regardless of direction: (is_missing, -value).
    return sorted(
        records,
        key=lambda r: (_sort_key(r, sort_by) is None, -(_sort_key(r, sort_by) or 0.0)),
    )
```

The sort key negates the value (`-(... )`), so **every** `sort_by` (including `"budget"`) orders **largest-first**. There is no branch for ascending order. When `sort_by="budget"` with `limit=2`, this returns the two **highest** budgets — exactly On Stranger Tides ($380M) and At World's End ($300M). The docstring even states the intent: "Sort descending with `None` values last."

The default sort is PES, also descending (`_DEFAULT_SORT: SortBy = "pes"` in `query_movies.py`; the GSI path returns PES-descending, see below). So whether the model set `sort_by="budget"` or left it unset, the top-of-list items are the extreme-high ones, never the low ones.

### 2. The tool SPEC the model sees exposes no sort direction and no "lowest" affordance

`src/movieintel/agent/tools/specs.py`, `QUERY_MOVIES_SPEC.inputSchema`:

- `sort_by` enum = `["pes", "revenue", "budget", "runtime"]` — a field to sort **on**, with **no** companion direction/order field.
- Budget controls are `min_budget` and `max_budget` (both `{"type": "number"}`) — **range filters only**, not ordering.
- `additionalProperties: false`, mirrored by `QueryMoviesArgs` with `model_config = ConfigDict(extra="forbid")` in `src/movieintel/agent/tools/args.py`. So the model literally **cannot** pass an invented `sort_direction` or `order` argument — strict validation (`validate_args`) would reject it as a `ToolValidationError`.

The spec `description` reinforces a filter+PES-ranking mental model: "Filter enriched movies ... optionally ranked by Production Effectiveness Score." Nothing signals that ascending/lowest ordering is possible, because it is not.

### 3. The repository has no ascending-budget access pattern either

`src/movieintel/persistence/repository.py`:

- `query_by_sentiment()` is the only index-backed ranked read. It hard-codes `"ScanIndexForward": False`, i.e. GSI1SK (zero-padded PES) **descending** — highest PES first. There is no `ScanIndexForward=True` path and no budget GSI.
- GSI1 is keyed on sentiment + PES only. `src/movieintel/persistence/keys.py` encodes `GSI1SK = PES#<zero-padded PES>` (`pes_gsi1sk`); there is **no budget sort key** anywhere, so DynamoDB cannot order by budget at the storage layer.
- `scan_numeric_range()` applies budget/revenue/runtime as a DynamoDB `FilterExpression` (`_numeric_range_filters` builds `Attr("budget").gte/lte(...)`). It **filters** by numeric bounds but returns items **in scan order** with no sort. Ordering is then re-imposed in Python by `_sort_records` — which, per finding 1, is descending-only.

So at every layer (spec → args model → tool sort → repository → GSI design) the system can rank highest-first but has no mechanism to produce lowest-first.

### 4. `compare_movies` is deterministic given ids — it is not the culprit

`src/movieintel/agent/tools/compare_movies.py`, `compare_movies()`:

- Takes `movie_ids` from the model, calls `repository.batch_get(validated.movie_ids)`, and computes min/max per dimension deterministically (`_compare_dimension`, ties broken by requested id order).
- It makes **no selection decisions** — it compares exactly the ids it is handed. The spec (`COMPARE_MOVIES_SPEC`) requires only `movie_ids` and optional `dimensions`; `CompareMoviesArgs` requires `movie_ids` (min length 2).

The agent's observed output (`kind=comparison`, `subjects=["280","1771"]`-style ids, dimensions budget/revenue/runtime, narrative) maps to `ComparativeAnalysis` in `src/movieintel/agent/response.py`, whose `subjects` are just the ids the model chose. The wrong movies therefore originate **upstream** in the model's `query_movies` → id-selection step, not in `compare_movies`.

### 5. The system prompt biases toward PES / high-value framing

`src/movieintel/agent/loop.py`, `build_agent_system_prompt()`:

- Describes `query_movies` as: "filter movies by sentiment and numeric ranges, **ranked by Production Effectiveness Score (PES)**."
- Describes `compare_movies` as a "deterministic side-by-side comparison of specific movies."
- Tells the model to "Make at most two tool-search attempts" and to prefer answering from results already in hand over "hunting for a better set."

Combined effect: the model is primed to think in terms of PES ranking (a high-value/blockbuster-leaning metric), is given no notion of ascending ordering, and is discouraged from iterating. For "lowest budget," the only levers it has are (a) `sort_by="budget"` → returns highest-first, or (b) `min/max_budget` filtering, which still needs an ordering step it cannot request. Picking the top-of-list items is the path of least resistance the tools steer it toward. (This is an LLM loop, so the exact calls are nondeterministic — but every available faithful path leads to highest-first, which is why the output is reliably inverted.)

### Verified vs inferred

- **Verified in code:** descending-only sort (finding 1); spec/args have no direction field and only min/max filters (finding 2); repository/GSI have no ascending or budget-ordered path (finding 3); `compare_movies` is deterministic over given ids (finding 4); system-prompt PES framing (finding 5).
- **Inferred (not deterministic):** the exact tool-call sequence the model used. Because it is an LLM, I frame the root cause as "the tools make 'lowest budget' **unexpressible** and default to highest-first" rather than asserting one fixed code path. The data facts (97 movies, budgets $11M–$380M) came from the orchestrator's live verification, not re-queried here.

## Conclusions

1. **Root cause:** "lowest budget" is unexpressible with the current `query_movies` contract. The sort is descending-only (`query_movies.py:_sort_records`), the spec offers no sort direction and only min/max filters (`specs.py:QUERY_MOVIES_SPEC`), and no repository access pattern orders ascending or by budget (`repository.py`, `keys.py`). The model's best available move returns highest-first results.
2. **No faithful path exists** for the model to retrieve the lowest-budget pair with today's tools. The missing capability is an **ascending / "lowest-first" ordering** on `query_movies` (equivalently, a sort direction).
3. **`compare_movies` is not at fault.** It deterministically compares whatever ids it receives; the wrong subjects were selected upstream.
4. **The system prompt compounds the issue** by advertising PES ranking and omitting any ascending-order affordance.

## Fix options (design-level; nothing implemented)

**Option A — Add a `sort_direction` (or `order`) parameter to `query_movies` (RECOMMENDED).**
- Where: add `sort_direction: {"enum": ["asc","desc"]}` (default `"desc"`) to `QUERY_MOVIES_SPEC.inputSchema` in `specs.py`; add the matching field to `QueryMoviesArgs` in `args.py`; honor it in `_sort_records`/`_sort_key` in `query_movies.py` (branch the sign of the sort key, keep `None` last in both directions). Update the contract test in `tests/contract/test_tool_specs.py` and the system-prompt wording in `loop.py`.
- Tradeoffs: smallest change that makes "lowest/highest X" directly expressible for budget, revenue, and runtime at once. For the sentiment-GSI path (PES-ranked) an ascending request would need a Python re-sort of the small result set (already how non-PES sorts work), so no new index is required. Does not touch the GSI design.
- Why recommended: it is the minimal, general fix that removes the exact missing capability (ascending order) at the layer where the model expresses intent, fixes all three numeric dimensions, and needs no DynamoDB schema change (the small-N dataset makes an in-Python ascending sort correct and cheap).

**Option B — Prompt-only guidance.** Reword `build_agent_system_prompt` to tell the model it can filter by `max_budget` to approximate "cheapest." Tradeoff: brittle and still can't truly order ascending; the model would have to guess a budget threshold. Not a real fix. Not recommended alone.

**Option C — Dedicated ordering/extremes capability.** Add a tool (or `query_movies` mode) that returns the N lowest/highest by a given dimension. Tradeoff: more surface area and a new spec/handler/tests; overkill given Option A already expresses this with one enum field.

**Option D — Storage-level ascending.** Add a budget GSI or expose `ScanIndexForward=True`. Tradeoff: largest change, unnecessary at 50–100 items where an in-Python sort is fine (per the design's documented small-N tradeoff in `repository.scan_numeric_range`). Not recommended.

**Recommendation:** Option A. Add a `sort_direction` enum (default `desc` to preserve current behavior) to the `query_movies` spec + `QueryMoviesArgs` + `_sort_records`, sync the contract test, and add one line to the system prompt noting that results can be ordered ascending or descending. This makes "lowest budget" expressible with the least change and no schema impact.
