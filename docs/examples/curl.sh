#!/usr/bin/env bash
# MovieIntel serving API examples (Task 10 FEAT-001, REQ-B-4.3).
#
# POSTs example queries to the deployed POST /query endpoint. Every body matches the
# QueryRequest schema (docs/openapi.yaml): a required non-empty "query" plus an optional
# "max_turns" override. Set API_URL to the deployed base URL (no trailing slash), e.g.:
#
#   API_URL="https://abc123.execute-api.us-east-1.amazonaws.com" ./curl.sh
#
# (The HTTP API uses the default stage, so the base URL has no stage path - no "/prod".)
#
# The third request intentionally sends a schema-violating body to demonstrate the
# structured HTTP 400 (the agent is never invoked for a bad request body).

set -euo pipefail

: "${API_URL:?set API_URL to the deployed serving endpoint base URL (no trailing slash)}"

# Note: the first synchronous call after an idle period can hit the cold-start 503 (see
# README section 8). Warm the endpoint with one throwaway POST before the real request. With
# the high-revenue-biased sample loaded, example 1 returns kind=recommendations (real
# positive-sentiment Action titles), not kind=bounded.
echo "== 0. Warm-up (throwaway, avoids the known cold-start 503) =="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST "${API_URL}/query" \
  -H "content-type: application/json" \
  -d '{"query": "warm up"}' || true
echo

echo "== 1. Recommendation query (expect HTTP 200, kind=recommendations) =="
curl -sS -X POST "${API_URL}/query" \
  -H "content-type: application/json" \
  -d '{"query": "Recommend action movies with high revenue and positive sentiment."}'
echo

echo "== 2. Preference-summary query (expect HTTP 200, kind=preferences) =="
curl -sS -X POST "${API_URL}/query" \
  -H "content-type: application/json" \
  -d '{"query": "Summarize the preferences of a viewer who enjoys high-revenue, positive-sentiment movies."}'
echo

echo "== 3. Comparison query with a max_turns override (expect HTTP 200, kind=comparison) =="
curl -sS -X POST "${API_URL}/query" \
  -H "content-type: application/json" \
  -d '{"query": "Compare the two highest-PES science-fiction films side by side.", "max_turns": 8}'
echo

echo "== 4. Schema-violating body: missing required 'query' (expect HTTP 400, error=invalid_request) =="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST "${API_URL}/query" \
  -H "content-type: application/json" \
  -d '{"not_query": "this body is missing the required query field"}'
