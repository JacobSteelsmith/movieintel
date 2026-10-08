#!/usr/bin/env bash
# MovieIntel serving API examples (REQ-B-4.3).
#
# Demonstrates both serving paths against the deployed HTTP API. Every body matches the
# QueryRequest schema (docs/openapi.yaml): a required non-empty "query" plus an optional
# "max_turns" override. Set API_URL to the deployed base URL (no trailing slash), e.g.:
#
#   API_URL="https://abc123.execute-api.us-east-1.amazonaws.com" ./curl.sh
#
# (The HTTP API uses the default stage, so the base URL has no stage path - no "/prod".)
#
# RECOMMENDED PATH - async submit-then-poll (POST /jobs -> poll GET /jobs/{id}): this is the
# 503-free path. The submit returns 202 quickly (well under the 30s gateway integration
# timeout) and a worker runs the agent loop to completion in the background, so a long query
# never hits the 30s wall. The synchronous POST /query example below it is the legacy path
# and can still return 503 on heavy queries.
#
# The last request intentionally sends a schema-violating body to demonstrate the structured
# HTTP 400 (the agent is never invoked, and no job is created, for a bad request body).

set -euo pipefail

: "${API_URL:?set API_URL to the deployed serving endpoint base URL (no trailing slash)}"

echo "== 1. Async submit-then-poll (RECOMMENDED, 503-free): POST /jobs -> poll GET /jobs/{id} =="
submit=$(curl -sS -X POST "${API_URL}/jobs" \
  -H "content-type: application/json" \
  --data '{"query": "Recommend action movies with high revenue and positive sentiment."}')
job_id=$(printf '%s' "$submit" | python3 -c 'import sys, json; print(json.load(sys.stdin)["job_id"])')
echo "submitted job_id=${job_id}"

body=""
status=queued
for _ in $(seq 1 150); do          # max-iteration guard (~180s at 1.2s sleeps)
  body=$(curl -sS "${API_URL}/jobs/${job_id}")
  status=$(printf '%s' "$body" | python3 -c 'import sys, json; print(json.load(sys.stdin)["status"])')
  [ "$status" = succeeded ] || [ "$status" = failed ] && break
  sleep 1.2
done
printf '%s\n' "$body"              # terminal AgentResult (succeeded) or error (failed)
echo

echo "== 2. Synchronous POST /query (LEGACY fallback, can still 503 on heavy queries) =="
# Note: the first synchronous call after an idle period can hit the cold-start 503 (see
# README section 8); the async path above does not. With the high-revenue-biased sample
# loaded, this returns kind=recommendations (real positive-sentiment Action titles).
curl -sS -X POST "${API_URL}/query" \
  -H "content-type: application/json" \
  -d '{"query": "Recommend action movies with high revenue and positive sentiment."}'
echo

echo "== 3. Schema-violating body: missing required 'query' (expect HTTP 400, error=invalid_request) =="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST "${API_URL}/jobs" \
  -H "content-type: application/json" \
  -d '{"not_query": "this body is missing the required query field"}'
