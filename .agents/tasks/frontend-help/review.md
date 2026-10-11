# Curated-examples help panel and contextual help for the web client

The web client gains a persistent "What can I ask?" panel and contextual help that surfaces on unsatisfying results. Both render from a single shared `HELP_EXAMPLES` const (six curated, kind-grouped questions), so the example strings have one source of truth. Clicking an example fills the query textarea and focuses it without submitting; the user still presses Ask. Contextual help reuses the same examples plus a friendly rephrase line and is appended to the existing `#status` aria-live region on refusal, bounded, empty recommendations, effectively-empty preferences/comparison, and the submit-side 400 validation path. The change is confined to `frontend/` (index.html, app.js, styles.css) and is purely additive to the existing flow.

Watch for: nothing blocking. The implementation holds the XSS boundary (no `innerHTML` introduced — confirmed), keeps the async submit-poll flow intact, and matches the six required strings exactly. Two non-blocking notes below (an extra pair of empty-state guards beyond the required four paths, and a stale "97 movies" count in a code comment).

**Verdict**: APPROVED

## High-level view

The six example strings are defined once in `HELP_EXAMPLES` and consumed by both the persistent panel (`renderHelpPanel` → `buildExampleList`) and the contextual help (`appendContextualHelp` → `buildExampleList`). There is genuinely one source of truth; the list builder is shared, so panel and contextual help can never drift. The six strings match the acceptance spec character-for-character, and no cast/actor or "lowest ranked" examples were invented.

Click-to-fill is a single shared handler (`fillQuery`) that sets `#query-input.value` and calls `focus()` with no submit path. Examples are real `<button type="button">` elements with text set via `textContent`/`createTextNode`, so they are keyboard-operable and announced.

Contextual help lands in the one existing aria-live region (`#status`) across all four required paths plus two additional empty-state guards (empty preferences, empty comparison) that were not required but are consistent and additive. Every DOM write goes through `createElement`/`textContent`; `innerHTML` appears only inside comments, so the stated XSS boundary is preserved.

The async submit-poll machinery, `renderResult`'s handling of the real result shapes, `warmUp`, the init placeholder guard, and `config.js` are untouched except for the additive contextual-help calls and a single `renderHelpPanel()` call in `init()`.

<details>
<summary>Issues (2)</summary>

1. **Extra empty-state guards (non-blocking)** — `renderPreferences` and `renderComparison` gained early-return contextual-help branches for effectively-empty results. The spec named four contextual-help paths (refusal, bounded, empty recommendations, 400); these two are additions. They are additive and consistent, not a regression, so no action required unless strict scope adherence is wanted.
2. **Stale movie count in comment (non-blocking)** — the `HELP_EXAMPLES` comment says "verified against the live data (97 movies)"; earlier task context referenced 100 ingested movies. It's a comment, not behavior, and out of scope for a frontend review, but worth a glance if the count matters.

</details>

<details>
<summary>Details</summary>

### Single source of truth for the six examples

`HELP_EXAMPLES` is an array of `{ group, query }` objects defined once near the top of `app.js`. `buildExampleList()` is the only place that turns the const into DOM, and both `renderHelpPanel()` (persistent panel) and `appendContextualHelp()` (contextual help) call it. That means the two surfaces cannot diverge: a change to the const or the builder updates both. The six `query` strings match the required set exactly, including punctuation:

```
"Recommend action movies with high revenue and positive sentiment."
"What are some lighthearted adventure movies?"
"Show me intense, high-budget movies."
"Summarize the preferences of a viewer who enjoys big-budget blockbusters."
"Compare the two highest-rated movies by Production Effectiveness Score."
"Compare the two movies with the lowest budgets."
```

Each supported `AgentResult` kind is represented via the `group` label (Recommendations, Preferences, Comparison). No examples reference cast/actors or a "lowest ranked" dimension, matching the constraint.

### Click-to-fill never submits

`fillQuery(query)` sets `#query-input.value` and calls `.focus()` with no form submission or event dispatch. Buttons are `<button type="button">`, which also prevents implicit submission even though the help panel lives outside the `<form>`. Discernible text is the group `<span>` (textContent) plus `createTextNode(example.query)` — all plain text nodes.

### Contextual help across the required paths

`appendContextualHelp(region, message)` builds a `<div class="contextual-help">` with a prompt paragraph (textContent) and the shared example list, then appends to the region. It is wired into:

- `renderResult` `case "refusal"` — after the neutral notice.
- `renderResult` `case "bounded"` — after the informational notice.
- `renderRecommendations` — early return when `movies.length === 0`.
- `onSubmit` 400 branch — appended to `statusRegion()` after the validation notice.

All four required paths route through the single `#status` region (`statusRegion()` returns `getElementById("status")`, the `role="status" aria-live="polite"` section). `renderNotice` clears the region before writing, and the help is appended afterward in the refusal/bounded/400 paths, so both notice and help stay visible.

Beyond the four, `renderPreferences` and `renderComparison` also append contextual help on effectively-empty results (no summary/highlights, or no dimensions/narrative). Not in the required list, but additive and consistent with the empty-recommendations behavior; the pre-existing `highlights`/`dimensions` declarations were only hoisted above the new guard, same values.

### XSS boundary preserved

Every node is created with `createElement` and populated with `textContent` or `createTextNode`. A grep for `innerHTML` across `frontend/` returns only four hits, all inside comments that describe the boundary; none assign to `.innerHTML`. The diff introduces no new `innerHTML`.

### Existing flow left intact

The diff touches `onSubmit` only to add the one `appendContextualHelp` call in the existing 400 branch and `init` only to add one `renderHelpPanel()` call after the submit listener is attached. `pollJob`, `scheduleRetry`, `stopPolling`, `warmUp`, the init placeholder guard, and `config.js` are unchanged. Because `renderHelpPanel()` sits after the placeholder guard's early return, the panel renders only on the configured path — an unconfigured client shows the config error and nothing else.

### Accessibility and build posture

The help panel is a labelled `<section aria-labelledby="help-heading">` below the form and status region inside `<main>`, with the matching `#help-heading`. The index.html comment notes these are baseline affordances and that full WCAG conformance needs manual AT testing and expert review — satisfying the required code comment. No new dependencies, no build step.

### Verification evidence

Not re-run in full. The commit message records `node --check frontend/app.js: PASS` and a grep showing `innerHTML` only in comments. A narrow spot-check confirmed the grep claim (four comment-only hits) and that the working tree has no uncommitted frontend changes, so the reviewed diff matches the committed state.

</details>

<details>
<summary>File map</summary>

- `frontend/app.js` — adds `HELP_EXAMPLES` const, `fillQuery`/`buildExampleList`/`renderHelpPanel`/`appendContextualHelp` helpers, contextual-help calls in `renderResult` (refusal, bounded), `renderRecommendations`, `renderPreferences`, `renderComparison`, and the `onSubmit` 400 path; one `renderHelpPanel()` call in `init`.
- `frontend/index.html` — adds the `#help-panel` labelled section below the status region.
- `frontend/styles.css` — adds styles for the help panel, shared example list/buttons, group badge, and contextual-help block.

Full diff: `git diff main...feature/frontend-help`

</details>
