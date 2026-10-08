// MovieIntel front end logic. Plain vanilla ES, no framework, no build step.
//
// Baseline accessibility (also documented in index.html): a labelled textarea, a
// real submit button, and a single aria-live status/results region, all
// keyboard-operable. Full WCAG conformance is NOT claimed from the code alone and
// requires manual assistive-technology testing and expert review.

"use strict";

// Read the deployer-provided base URL once. config.js runs before this script.
const API_BASE = window.API_BASE_URL;
const API_PLACEHOLDER = "https://REPLACE_ME.execute-api.us-east-1.amazonaws.com";

// The Mood enum has exactly six members (domain.schemas.Mood); the Sentiment enum
// has three (domain.schemas.Sentiment). Used only to pick a CSS class; any value
// still renders as text so an unexpected label is never silently dropped.
const KNOWN_MOODS = ["dark", "light", "intense", "uplifting", "tense", "lighthearted"];
const KNOWN_SENTIMENTS = ["positive", "negative", "neutral"];

// Client-side heuristic progress timeline. This is NOT backend telemetry: the API
// is a single synchronous request/response with a long (~22-23s) cold-start wait,
// so these messages are purely time-based guesses to reassure the user. Each entry
// is {at: millisecondsSinceSubmit, text}. The timeline is cleared the moment fetch
// resolves (success OR error), regardless of which stage is current.
const STAGES = [
  { at: 0, text: "Warming up the model..." },
  { at: 2000, text: "Understanding your question..." },
  { at: 6000, text: "Searching the movie catalog..." },
  { at: 14000, text: "Composing your answer..." },
];

let stageTimer = null;

function statusRegion() {
  return document.getElementById("status");
}

// Replace the status/results region with a single simple notice built entirely
// from textContent. `variant` selects styling (progress|error|"").
function renderNotice(message, variant) {
  const region = statusRegion();
  region.replaceChildren();
  const notice = document.createElement("div");
  notice.className = variant ? `notice ${variant}` : "notice";
  notice.textContent = message;
  region.appendChild(notice);
}

// Drive the client-side staged progress timeline. setTimeout per stage so each
// threshold writes its message into the aria-live region.
function startStages() {
  clearStages();
  const timers = STAGES.map((stage) =>
    window.setTimeout(() => renderNotice(stage.text, "progress"), stage.at),
  );
  stageTimer = timers;
}

function clearStages() {
  if (stageTimer) {
    stageTimer.forEach((id) => window.clearTimeout(id));
    stageTimer = null;
  }
}

function makeBadge(labelText, valueText, className) {
  const badge = document.createElement("span");
  badge.className = className ? `badge ${className}` : "badge";
  if (labelText) {
    const label = document.createElement("span");
    label.className = "badge-label";
    label.textContent = labelText;
    badge.appendChild(label);
  }
  // Append the value as a separate text node so nothing is set via innerHTML.
  badge.appendChild(document.createTextNode(valueText));
  return badge;
}

function sentimentClass(value) {
  return KNOWN_SENTIMENTS.includes(value) ? `sentiment-${value}` : "sentiment-neutral";
}

function moodClass(value) {
  return KNOWN_MOODS.includes(value) ? `mood-${value}` : "";
}

// Render one of the five AgentResult kinds (agent/response.py AgentResult union)
// plus a default branch. ALL untrusted model text is written via textContent /
// text nodes; innerHTML is never used anywhere. This is the XSS boundary.
function renderResult(body) {
  const region = statusRegion();
  region.replaceChildren();

  switch (body && body.kind) {
    case "recommendations":
      renderRecommendations(region, body);
      break;
    case "preferences":
      renderPreferences(region, body);
      break;
    case "comparison":
      renderComparison(region, body);
      break;
    case "refusal":
      // HTTP 200 refusal (RefusalResponse): neutral notice, no content.
      renderNotice(
        body.reason || "The request was declined and no content was produced.",
        "",
      );
      break;
    case "bounded":
      // HTTP 200 bounded (BoundedResponse): informational notice.
      renderNotice(
        `${body.reason || "The agent stopped before finishing."} (turns used: ${
          body.turns_used
        })`,
        "",
      );
      break;
    default:
      renderNotice("Unexpected response from the service.", "error");
      break;
  }
}

function renderRecommendations(region, body) {
  const heading = document.createElement("h2");
  heading.className = "result-heading";
  heading.textContent = body.query_summary || "Recommendations";
  region.appendChild(heading);

  const movies = Array.isArray(body.movies) ? body.movies : [];
  movies.forEach((movie) => {
    const card = document.createElement("article");
    // movie_id is the stable identity/key; not necessarily shown to the user.
    card.className = "card";
    if (movie && movie.movie_id != null) {
      card.dataset.movieId = String(movie.movie_id);
    }

    const title = document.createElement("h3");
    title.className = "card-title";
    title.textContent = movie && movie.title ? movie.title : "Untitled";
    card.appendChild(title);

    const badges = document.createElement("div");
    badges.className = "badges";
    if (movie && movie.sentiment != null) {
      badges.appendChild(
        makeBadge("sentiment: ", String(movie.sentiment), sentimentClass(movie.sentiment)),
      );
    }
    if (movie && movie.mood != null) {
      badges.appendChild(makeBadge("mood: ", String(movie.mood), moodClass(movie.mood)));
    }
    if (movie && movie.pes != null && !Number.isNaN(Number(movie.pes))) {
      // PES rendered to one decimal place.
      badges.appendChild(makeBadge("PES: ", Number(movie.pes).toFixed(1), "pes"));
    }
    card.appendChild(badges);

    if (movie && movie.rationale) {
      const rationale = document.createElement("p");
      rationale.className = "rationale";
      rationale.textContent = movie.rationale;
      card.appendChild(rationale);
    }

    region.appendChild(card);
  });
}

function renderPreferences(region, body) {
  const heading = document.createElement("h2");
  heading.className = "result-heading";
  heading.textContent = body.subject ? `Preferences: ${body.subject}` : "Preferences";
  region.appendChild(heading);

  const card = document.createElement("article");
  card.className = "card";

  if (body.summary) {
    const summary = document.createElement("p");
    summary.textContent = body.summary;
    card.appendChild(summary);
  }

  const highlights = Array.isArray(body.highlights) ? body.highlights : [];
  if (highlights.length > 0) {
    const list = document.createElement("ul");
    list.className = "highlight-list";
    highlights.forEach((item) => {
      const li = document.createElement("li");
      li.textContent = String(item);
      list.appendChild(li);
    });
    card.appendChild(list);
  }

  region.appendChild(card);
}

function renderComparison(region, body) {
  const heading = document.createElement("h2");
  heading.className = "result-heading";
  const subjects = Array.isArray(body.subjects) ? body.subjects : [];
  heading.textContent = subjects.length ? `Comparison: ${subjects.join(" vs ")}` : "Comparison";
  region.appendChild(heading);

  const card = document.createElement("article");
  card.className = "card";

  const dimensions = Array.isArray(body.dimensions) ? body.dimensions : [];
  if (dimensions.length > 0) {
    const badges = document.createElement("div");
    badges.className = "badges";
    dimensions.forEach((dim) => badges.appendChild(makeBadge("", String(dim), "")));
    card.appendChild(badges);
  }

  if (body.narrative) {
    const narrative = document.createElement("p");
    narrative.textContent = body.narrative;
    card.appendChild(narrative);
  }

  region.appendChild(card);
}

// Translate an HTTP response (or network failure) into a rendered state. The UI is
// never left stuck on "loading": every branch writes into the aria-live region.
async function handleResponse(response) {
  // 2xx -> parse and render one of the five AgentResult kinds.
  if (response.ok) {
    const body = await safeJson(response);
    if (body === undefined) {
      renderNotice("The service returned a response that could not be read.", "error");
      return;
    }
    renderResult(body);
    return;
  }

  if (response.status === 400) {
    // ValidationErrorResponse: {error: 'invalid_request', detail: [...]}.
    const body = await safeJson(response);
    const message = extractValidationMessage(body);
    renderNotice(message, "error");
    return;
  }

  if (response.status === 429) {
    renderNotice("The service is rate limited right now. Please wait a moment and try again.", "error");
    return;
  }

  if (response.status >= 500) {
    // Includes 503: typically a cold start still warming up.
    renderNotice("The service is still warming up. Please try your query again.", "error");
    return;
  }

  renderNotice(`The service returned an unexpected status (${response.status}).`, "error");
}

// Pull a readable message out of a 400 ValidationErrorResponse detail list; fall
// back to a generic message if the shape is unexpected.
function extractValidationMessage(body) {
  if (body && Array.isArray(body.detail)) {
    const messages = body.detail
      .map((entry) => (entry && typeof entry.msg === "string" ? entry.msg : null))
      .filter((msg) => msg);
    if (messages.length > 0) {
      return `Your request was invalid: ${messages.join("; ")}`;
    }
  }
  return "Your request was invalid. Please adjust it and try again.";
}

// Parse JSON but never throw on a non-JSON body; returns undefined on failure.
async function safeJson(response) {
  try {
    return await response.json();
  } catch (err) {
    console.error("Failed to parse response body as JSON:", err);
    return undefined;
  }
}

// Advisory warmup: ping GET /health on load to trigger the Lambda cold start early.
// Failures NEVER surface as a user error; they are swallowed to the console and the
// form stays fully usable. Warmup never blocks submission.
function warmUp() {
  fetch(API_BASE + "/health", { method: "GET" })
    .then((response) => {
      if (response.ok) {
        renderNotice("Service is warm and ready.", "progress");
      } else {
        renderNotice("Service is warming up; your first query may be slower.", "progress");
      }
    })
    .catch((err) => {
      console.error("Advisory warmup failed (ignored):", err);
      renderNotice("Service is warming up; your first query may be slower.", "progress");
    });
}

async function onSubmit(event) {
  event.preventDefault();

  const input = document.getElementById("query-input");
  const button = document.getElementById("submit-button");
  const text = input.value.trim();

  if (!text) {
    renderNotice("Please enter a question before submitting.", "error");
    return;
  }

  button.disabled = true;
  startStages();

  try {
    // Send ONLY {query}. QueryRequest is extra='forbid', so sending max_turns (or
    // anything else) would trip a 400. Any future max_turns control MUST source its
    // bound from serving.schemas.MAX_TURNS_CAP, not a magic literal.
    const response = await fetch(API_BASE + "/query", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query: text }),
    });
    await handleResponse(response);
  } catch (err) {
    // fetch rejects with a TypeError on network failure or a CORS block.
    console.error("Request failed (network/CORS):", err);
    renderNotice("Could not reach the service. Check your connection and try again.", "error");
  } finally {
    clearStages();
    button.disabled = false;
  }
}

function init() {
  // If the base URL is missing or still the placeholder, render an error and do
  // NOT fire any requests (the deployer has not wired config.js yet).
  if (!API_BASE || API_BASE === API_PLACEHOLDER) {
    renderNotice(
      "The front end is not configured yet: API_BASE_URL has not been set by the deployer.",
      "error",
    );
    return;
  }

  const form = document.getElementById("query-form");
  form.addEventListener("submit", onSubmit);
  warmUp();
}

document.addEventListener("DOMContentLoaded", init);
