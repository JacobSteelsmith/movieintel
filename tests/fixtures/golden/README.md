# Golden set — hand-labeled evaluation fixtures (REQ-X-2.1)

`golden_set.json` is a small, self-contained, hand-labeled subset used by the Task 4
evaluation harness (`src/movieintel/eval/`). It does **not** read the real `_sqlite/`
databases at runtime — every input needed to compute the deterministic PES and to
evaluate the LLM-reasoned fields is carried inline.

Each item carries:

- Movie inputs compatible with `data_access.models.MovieRow`: `movie_id`, `title`,
  `overview`, `budget`, `revenue`, `genres`, `language`, `release_date`, `runtime`.
- `rating` (float or null) and `rating_count`, plus `effective_max` (the data-derived
  rating scale from Task 1, 5.0 here).
- Expected labels: `expected_sentiment` (a `Sentiment` value), `expected_mood` (a `Mood`
  enum member), and `expected_pes` (the deterministic PES value).

## PES-independence (REQ-X-2.5) — how each `expected_pes` was derived

The `expected_pes` literals below were computed **by hand from the design §3.2 formula**,
NOT by calling the production `movieintel.domain.pes` function. The formula is:

```
rating_norm = clamp(rating / effective_max, 0, 1)          # 0.0 when rating is null
if budget is None or budget <= 0:                          # rating-only safe branch
    value = round(100 * rating_norm, 2)
else:
    roi      = (revenue_or_0 - budget) / budget            # revenue null -> treated as 0
    roi_norm = clamp(1 / (1 + e^(-roi)), 0, 1)             # logistic
    value    = round(100 * (0.6 * roi_norm + 0.4 * rating_norm), 2)
```

A test (`tests/eval/test_harness.py::test_expected_pes_literals_are_independent`)
cross-checks every literal against `tests/domain/_reference_pes.py` — an independent
reference implementation that also does **not** import the production PES module — so the
100%-exactness check stays non-circular.

Per-item arithmetic (`e` = Euler's number, `logistic(x) = 1 / (1 + e^(-x))`):

| id  | branch | arithmetic | expected_pes |
|-----|--------|-----------|--------------|
| 101 | full ROI + rating | roi = (3,000,000 − 1,000,000) / 1,000,000 = 2.0; logistic(2.0) = 0.880797…; rating_norm = 4.0 / 5.0 = 0.8; round(100 · (0.6·0.880797… + 0.4·0.8), 2) | **84.85** |
| 102 | full ROI + rating (ROI saturates) | roi = (200,000,000 − 5,000,000) / 5,000,000 = 39.0; logistic(39.0) ≈ 1.0; rating_norm = 4.6 / 5.0 = 0.92; round(100 · (0.6·1.0 + 0.4·0.92), 2) | **96.8** |
| 103 | budget = 0 → rating-only | rating_norm = 2.0 / 5.0 = 0.4; round(100 · 0.4, 2) | **40.0** |
| 104 | budget null → rating-only | rating_norm = 3.5 / 5.0 = 0.7; round(100 · 0.7, 2) | **70.0** |
| 105 | rating null (full ROI branch) | roi = 2.0; logistic(2.0) = 0.880797…; rating_norm = 0.0; round(100 · (0.6·0.880797… + 0.4·0.0), 2) | **52.85** |
| 106 | no evidence (budget null + rating null) | rating_norm = 0.0; rating-only branch → round(100 · 0.0, 2) | **0.0** |
| 107 | negative ROI (flop) + low rating | roi = (10,000,000 − 100,000,000) / 100,000,000 = −0.9; logistic(−0.9) = 0.289050…; rating_norm = 1.5 / 5.0 = 0.3; round(100 · (0.6·0.289050… + 0.4·0.3), 2) | **29.34** |
| 108 | revenue null with budget > 0 (ROI defined, negative) | roi = (0 − 2,000,000) / 2,000,000 = −1.0; logistic(−1.0) = 0.268941…; rating_norm = 4.0 / 5.0 = 0.8; round(100 · (0.6·0.268941… + 0.4·0.8), 2) | **48.14** |

The set deliberately spans every PES branch exercised in `tests/domain/test_pes.py`:
the full ROI+rating branch (101, 102, 107, 108), the budget ≤ 0 / null rating-only branch
(103, 104), the missing-rating branch (105), and the no-evidence 0.0 edge (106).
