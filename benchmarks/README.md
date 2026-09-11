# Rotor benchmark MVP

This package runs a small, reproducible HTTP benchmark against either a Rotor
gateway or a provider directly.  It uses `httpx` and the wire protocols only;
the optional `openai` and `anthropic` SDK extras are not needed.
The package remains self-contained and SDK-free.  In addition to running a
round, it includes a scriptable direct-versus-gateway comparison and optional
process resource sampling; neither feature changes the gateway or requires a
production database connection.

Run commands from the `rotor` project directory (or from an installed wheel):

```bash
# deterministic local upstream
python -m benchmarks.mock_provider --port 9001 --first-chunk-delay 0.02 --chunk-interval 0.01

# non-streaming Chat (direct mock)
python -m benchmarks.runner --base-url http://127.0.0.1:9001 \
  --api-key benchmark-key --model mock-model --protocol chat \
  --no-stream --concurrency 2 --requests 10 --warmup 2 \
  --output-dir benchmarks/results/chat-json

# streaming all three protocol surfaces (Rotor root URL shown)
python -m benchmarks.runner --base-url http://127.0.0.1:8000 \
  --api-key "$ROTOR_API_KEY" --model glm-5.2 --protocol all \
  --stream --concurrency 10 --requests 100 --warmup 5

# repeat a finite scenario independently (three round directories plus an
# aggregate.json/summary.json at the parent)
python -m benchmarks.runner --base-url http://127.0.0.1:8000 \
  --api-key "$ROTOR_API_KEY" --model glm-5.2 --protocol chat \
  --stream --concurrency 10 --requests 100 --warmup 5 --rounds 3 \
  --output-dir benchmarks/results/gateway/chat-rounds

# duration mode (the requests value is only a compatibility placeholder)
python -m benchmarks.runner --base-url http://127.0.0.1:8000 \
  --api-key "$ROTOR_API_KEY" --model glm-5.2 --protocol chat \
  --stream --concurrency 10 --duration 30 --warmup 5 \
  --output-dir benchmarks/results/gateway/chat-duration

# runner-integrated CPU/RSS sampling for a Rotor process
python -m benchmarks.runner --base-url http://127.0.0.1:8000 \
  --api-key "$ROTOR_API_KEY" --model glm-5.2 --protocol chat \
  --no-stream --concurrency 4 --requests 100 --warmup 5 \
  --pid "$ROTOR_PID" --sample-interval 0.5 \
  --output-dir benchmarks/results/gateway/chat-with-resources

# Compare two completed rounds (gateway minus direct); JSON is the default.
python -m benchmarks.compare \
  benchmarks/results/direct/chat \
  benchmarks/results/gateway/chat \
  --format markdown --output benchmarks/results/chat-comparison.md
```

`--protocol` accepts `chat`, `responses`, `anthropic`, or `all`.  The client
constructs `/v1/chat/completions`, `/v1/responses`, and
`/anthropic/v1/messages` for a Rotor root; a base ending in `/v1` is treated as
a direct provider base.  Explicit endpoint paths are not duplicated.  Every
request gets a fresh `X-Request-Id`.  By default a fixed conversation ID is
used for a client; pass `--conversation-id` to choose it or
`--random-conversation-id` to generate one per request.
`--conversation-mode random` is an equivalent CLI spelling.  `--database-url`
is optional metadata only (it is redacted and never opened by the runner).
Streaming Chat requests ask direct OpenAI-compatible providers for usage with
`stream_options.include_usage=true`; providers that do not support that field
may legitimately produce a null usage sample.

## Measurement semantics

Concurrency is **closed-loop**: at most `--concurrency` workers are active and
each worker starts its next request after the previous one completes.  The
runner also sets the self-created httpx connection pool's
`max_connections`/`max_keepalive_connections` to that concurrency, so values
above httpx's default 100 are measured without an implicit client-side cap
(an injected `http_client` retains its own limits).
optional `--rate N` caps launch times at approximately N requests/second; it
does not change the concurrency limit and is called out separately in
`report.md`.  Warmup requests run before the measured wall-clock window and
are discarded from `raw-results.jsonl`, summary counts, and RPS.  They are
still real gateway requests and may be written to Rotor's database; usage
snapshots must therefore be taken after warmup (or scoped with an explicit
`--since` time).

`--rounds N` repeats the same measured scenario in independent measured
windows.  The rounds reuse the same `BenchmarkClient` and, by default, its
fixed conversation strategy/session identifier; use `--random-conversation-id`
when each request must be isolated to its own conversation.  When `N > 1` the
parent directory contains one `round-XXX/` artifact set per round and an
aggregate.  Aggregate elapsed time and throughput combine the measured
windows (for duration rounds this is the sum of those windows); request-level
P90/chunk/output-token quantiles are not reconstructed, and aggregate `max`
is `null` because only per-round P99 values are retained.

`--duration SECONDS` is an alternative to a finite `--requests` count and
takes precedence when both are supplied.  Duration rounds stream every safe
raw row to disk but retain only the first 4,096 rows for quantiles.  The
summary exposes `quantile_sample_limit`, `quantile_sample_size`, and
`quantiles_exact`; values beyond the bound are counted for totals/RPS but are
not included in P50/P95/P99 calculations.  The placeholder `requests` value
is recorded as `null` and ignored by scenario comparison, so different
placeholders do not make equal-duration runs incomparable.

Streaming is successful only after SSE data through the protocol terminator has
been consumed and the terminator has been observed.  The client then closes
the response instead of waiting for arbitrary bytes after a terminator (which
prevents a misbehaving upstream from holding a benchmark worker forever):

* OpenAI Chat: `data: [DONE]` (compatible `done` marker is accepted).
* OpenAI Responses: `response.completed`; `response.failed`/`error` fail.
* Anthropic Messages: `message_stop`; `error`/`stream_error` fail.

TTFT is the time from request start to the first non-empty text delta, not to a
role/preamble event.  Chunk intervals are inter-arrival times of parsed,
non-terminal SSE data events.  Result files contain only IDs, status, timings,
counts, and normalized usage; response text, request bodies, headers, and API
keys are never written.  A supplied `X-Conversation-Id` is sent unchanged in
the request header, but persisted raw rows contain only a stable
`sha256:<prefix>` fingerprint.  Percentiles use explicit linear interpolation over
the sorted `n-1` interval and return `null` for empty samples.  `summary.json`
also includes HTTP `status_codes` and normalized `errors` distributions.

Each measured round contains:

```text
environment.json   # git/Python/package/OS/CPU, redacted target and parameters
raw-results.jsonl  # one safe record per measured request
summary.json       # success/error/RPS/latency/TTFT/chunk/token metrics
report.md          # human-readable interpretation and measurement units
```

An existing output directory is never overwritten; a `-1`, `-2`, …
suffix is selected unless an explicit caller uses `overwrite=True` through the
Python API.

## Mock provider knobs

`MockProviderConfig` and the CLI support `--first-chunk-delay`,
`--chunk-interval`, `--chunk-count`, `--status-code` (including 429/500/503),
`--response-text`, `--input-tokens`, and `--output-tokens`.  The corresponding
environment variables are `MOCK_FIRST_CHUNK_DELAY`, `MOCK_CHUNK_INTERVAL`,
`MOCK_CHUNK_COUNT`, `MOCK_STATUS_CODE`, `MOCK_RESPONSE_TEXT`,
`MOCK_INPUT_TOKENS`, and `MOCK_OUTPUT_TOKENS`.  For repeatable failover tests,
`--fail-after N` starts an injected failure at request sequence `N+1`, and
`--recover-after M` ends it after sequence `M` (for example,
`--fail-after 2 --recover-after 4` gives success, success, failure, failure,
then success).  A recovery value without `--fail-after` starts failures at
sequence 1.  Sequence failures use status 503 by default; choose another
status with `--failure-status-code` (or `MOCK_FAIL_AFTER`,
`MOCK_RECOVER_AFTER`, and `MOCK_FAILURE_STATUS_CODE`).  Their JSON error has
the explicit `injected_sequence_failure` category.  The original fixed
`--status-code` mode remains unchanged and takes precedence over the sequence
schedule.  The FastAPI `app` and `create_app()` are usable with
`httpx.ASGITransport`, so tests need no external network.  `first_chunk_delay`
is applied immediately before the first useful text event (after protocol
metadata preambles) and also before non-streaming JSON responses, so a small
client timeout can exercise deterministic upstream timeouts in either mode.

## Direct/gateway comparison

`benchmarks.compare` accepts either result directories or individual
`summary.json`/`raw-results.jsonl` files:

```bash
python -m benchmarks.compare DIRECT_ROUND GATEWAY_ROUND \
  --format json --output comparison.json
# Use --format markdown for a reviewable table, or --allow-incomparable only
# when intentionally comparing different scenarios.
```

The tool checks protocol, stream mode, model, concurrency, request/warmup
counts, timeout/rate, max output, prompt fingerprint, conversation strategy,
and any duration/round controls present in both environments.  In duration
mode, `duration_seconds_requested` is the workload control; legacy `requests`
placeholders are ignored when both rounds record a duration.  A conflicting
value is never silently merged: the JSON contains `comparable: false`, lists
the exact mismatch, and the CLI exits with status 3 unless
`--allow-incomparable` is supplied.  Bare summaries with no scenario metadata
are marked `scenario_verified: false`; use `--strict-scenario` when missing
metadata must fail.  Strict verification requires the minimal identity set
`protocol`, `stream`, `model`, `concurrency`, and one common workload control
(`requests` or `duration_seconds_requested`).  Other controls are compared
when present but are not required solely for compact hand-written summaries;
scenario values and mismatch details are bounded and redacted in output.
Deltas are always `gateway - direct` and include latency
and TTFT P50/P95/P99, RPS, successful RPS, success/error rates, and input,
output, total, and usage-covered request differences.  Only whitelisted,
redacted metadata is emitted; prompts, response text, request rows, headers,
API keys, and arbitrary environment values are omitted.

## Optional process resource sampling

Process sampling is independent of the runner and is intentionally optional:

```bash
python -m benchmarks.resource_sampler --pid "$ROTOR_PID" \
  --interval 0.5 --duration 30 --format json \
  --output benchmarks/results/gateway/resources.json
```

The Python API supports `ProcessSampler(pid, interval=...)` with
`start()`/`stop()` around a benchmark, or deterministic `collect()` calls in
tests.  A mockable `source(pid)` can return a `(cpu_percent, rss_bytes)` pair
or a mapping.  `ResourceStats` reports status, average/max CPU percentage,
average/peak RSS bytes, sample count, interval, and explicit units.  If the
PID has exited, permissions are insufficient, or the host is unsupported, the
result is a clear `status: "unavailable"` record and the benchmark itself is
not failed.  A background sampler requires a strictly positive interval (zero
is accepted only for finite synchronous `collect(samples=...)` probes); a
zero-sample request returns `status: "unavailable"`, `sample_count: 0`, and
`reason: "no samples requested"`.  `psutil` is used when installed; otherwise macOS/Linux `ps` is
used, with no new mandatory dependency.  The sampler requests numeric CPU/RSS
columns only and never records process command lines, arguments, environment,
or credentials.  Treat resource numbers as observational (sampling can miss
short spikes); report the host OS and sampling interval with any comparison.

## Usage validation

Take a read-only snapshot before and after a measured round:

```bash
python -m benchmarks.validate_usage --db "$DATABASE_URL" \
  --snapshot-out /tmp/before.json
# run warmup first (or choose a --since timestamp immediately before the
# measured requests), then run the measured benchmark
python -m benchmarks.validate_usage --db "$DATABASE_URL" \
  --snapshot-out /tmp/after.json
python -m benchmarks.validate_usage --before /tmp/before.json --after /tmp/after.json
```

The validator accepts a filesystem path or `sqlite:///...` URL and opens it
with SQLite `mode=ro` (including a live WAL).  It reads only safe columns from
`request_logs`, `usage_ledger`, `request_attempts`, `tokens`, and `channels`;
the `tokens.key` and request/response body columns are never selected.  Rotor's
current `request_logs` table has no `request_id`, so request-id filters are
joined against ledger/attempt rows and log totals are checked by aggregate or
time-window deltas; this limitation is reported in the comparison output.
Mutable cumulative rows in `tokens`/`channels` are retained in full for both
snapshots so an old creation timestamp cannot erase the quota baseline; the
requested time window applies to append-only request/ledger/attempt rows as a
half-open interval `[since, until)` (an attempt is included when its execution
interval overlaps that range).
The retained request/conversation/user IDs are operational audit identifiers,
not credentials; keep snapshot JSON protected and do not publish it.
If a core accounting table (`request_logs`, `usage_ledger`, or `tokens`) is
absent in either snapshot, the comparison reports `checks_complete=false` and
exits unsuccessful rather than treating a zero delta as proof of correct
accounting.  Missing supplementary `request_attempts`/`channels` tables are
reported as incomplete evidence for those checks.
The comparison also flags duplicate newly observed `usage_ledger.request_id`
rows.  With explicit `--request-id` filters, global `request_logs` and
`tokens.used_quota` equality checks are marked skipped because the current
schema cannot attribute those counters to one request.
Unreadable or non-existent databases fail clearly and no database mutation is
attempted.  Use separate `DATABASE_URL` and `CONVERSATION_STORE_DIR` values for
the gateway under test and the benchmark/mock environment.  For PostgreSQL,
record the database type in the report and use an isolated test database or a
read-only SQL snapshot workflow; this MVP's direct snapshot reader is SQLite
only.  Real upstream runs should use a dedicated key/model and account for
provider rate limits and billing.

## Python API

`run_benchmark(...)` in `runner.py` is the programmatic entry point.  The
`clients.py` helpers (`build_*_payload`, `parse_sse`, `terminal_status`) are
small and deterministic, which makes protocol contract tests straightforward.
