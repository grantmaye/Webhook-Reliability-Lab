# Webhook Reliability Lab: technical manual

This lab pairs a Node.js receiver with a Python delivery client to demonstrate signed delivery, bounded retries, and durable duplicate handling. Read the [product story](product-story.md) for its hypothetical use case. It is a local experiment with synthetic orders, not a live provider integration or exactly-once business-processing system.

## 1. Learn the vocabulary

A **webhook** is an HTTP request sent when an event occurs. The sender may not know whether the receiver committed an event if the response is lost. Retrying is necessary, but retries create duplicates unless the receiver recognizes event identity.

**HMAC** is a keyed message authentication code: both parties share a secret and compute a digest from the same bytes. It proves possession of the secret and message integrity, not confidentiality. A timestamp limits how long an intercepted signature can be replayed. An **idempotent inbox** saves an event once and acknowledges identical deliveries without inserting again. **Backoff** spaces retries after failures; **jitter** adds randomness so many clients do not retry simultaneously.

An **acknowledgment** confirms this particular event, not merely that some web server returned HTTP 200. The sender requires the correct event ID and a valid duplicate flag matching the receiver's status. Missing, malformed, or mismatched acknowledgments leave the outcome uncertain and are retried within the budget.

## 2. Run the entire system

Requirements: Node.js 24.x and Python 3.11+ on PATH. There are no third-party runtime packages or package installation commands.

```bash
node --version
python3 --version
npm test
python3 -m unittest discover -s tests -v
python3 scripts/demo.py
```

If your system's `python3` is an unavailable OS developer-tool shim, invoke an installed compatible interpreter explicitly, such as `python3.11`, for these same commands. This changes no application contract.

[The demo](../scripts/demo.py) creates a random secret and temporary SQLite file, starts Node on port 0 (OS-assigned loopback port), and waits for its JSON listening message. It requests two deliberate failures, then delivers the [two sample events](../examples/events.json), replays both, and tries changed content with the first ID. It checks both the authenticated diagnostic endpoint and SQLite itself before shutting down and deleting the temporary directory.

Expected: first event succeeds on attempt 3, second succeeds once, replays are duplicates, changed content returns 409 after one attempt. Final counters are attempts=7, accepted=2, duplicates=2, transient_failures=2, stored_events=2. The conflict counts as a validated attempt but not accepted/duplicate. These are assertions from a synthetic run, not production delivery statistics.

## 3. Follow the code and data flow

| Source | Responsibility |
| --- | --- |
| [src/server.js](../src/server.js) | Configuration, persistent inbox, loopback listener, shutdown |
| [src/receiver.js](../src/receiver.js) | Raw-body reader, signatures, schema, fault injection, inbox transaction, HTTP routes |
| [sender/delivery.py](../sender/delivery.py) | Stable encoding, HMAC headers, HTTP transport, retry policy, Delivery result |
| [sender/__main__.py](../sender/__main__.py) | CLI input, per-event output, exit codes |
| [scripts/demo.py](../scripts/demo.py) | Cross-language subprocess/HTTP/SQLite demonstration |
| [test/receiver.test.js](../test/receiver.test.js) | Node signatures, persistent inbox, concurrent HTTP deliveries, rollback |
| [tests/test_sender.py](../tests/test_sender.py) | Deterministic Python retry and acknowledgment cases |
| [.github/workflows/ci.yml](../.github/workflows/ci.yml) | Node 24, Python 3.12, both suites, complete demo |

```text
Python event → stable JSON bytes → timestamp + HMAC → HTTP POST
  → Node bounded raw body → signature/freshness → parse/schema
  → optional injected 503 → inbox transaction → event acknowledgment
  → Python verifies acknowledgment or schedules bounded retry
```

The order matters: simulated failures only occur after signature and event-schema validation. Invalid signatures cannot consume the deliberate failure budget. Successful retries keep the event bytes stable but sign a fresh timestamp on every attempt. Parsing/reserializing before signature verification would authenticate different bytes from what arrived.

## 4. Exact wire contract

POST `/webhooks/orders` accepts at most 65,536 bytes with application/json:

```json
{"id":"evt_1001","type":"order.created","data":{"order_id":"ORD1001","amount_cents":12999,"currency":"USD"}}
```

Both IDs require 1–100 letters/digits/underscores/hyphens. Only order.created and USD are supported. Amount must be a nonnegative JavaScript safe integer. No real payment is performed. Extra event fields are not stripped; the raw bytes still contribute to the deduplication digest.

Headers are X-Webhook-Timestamp (ten decimal digits, Unix seconds) and X-Webhook-Signature (64 lowercase hex characters). Signature input is UTF-8 timestamp, a literal dot, then the **exact raw body**. HMAC-SHA256 uses WEBHOOK_SECRET. Node verifies format and freshness within 300 seconds in either direction and uses timingSafeEqual for the digest. At subsecond clock precision the absolute age comparison still applies; test clocks use exact millisecond values.

| Status | Meaning and Python behavior |
| --- | --- |
| 202 plus matching event_id/duplicate=false | New event saved; delivered=true |
| 200 plus matching event_id/duplicate=true | Same event already saved; delivered=true |
| Any 2xx with missing/wrong/malformed acknowledgment | Unknown outcome; retry same bytes; eventual invalid_acknowledgment if exhausted |
| 400 / 401 / 409 / 413 / 415 / 422 | Malformed JSON, bad signature, ID conflict, size, media type, or schema; stop |
| 408 / 429 / 500 / 502 / 503 / 504 | Potentially transient; retry within budget |
| Other HTTP status, including redirects | Permanent for this client; stop |
| Network timeout/failure | Unknown outcome; retry same bytes |

A valid new response is `{"event_id":"evt_1001","duplicate":false}`. A duplicate changes false to true and status to 200. The client deliberately implements this receiver-specific contract; it is not a universal webhook provider SDK. A successful inbox acknowledgment does not mean downstream fulfillment has happened.

The Python serializer sorts keys, removes optional JSON spacing, and rejects NaN. That produces stable bytes across retries. Its preflight requires a nonempty string ID, valid HTTP(S) endpoint without credentials/fragments, a sufficiently long secret, an attempt count from 1–8, and a body within the byte limit. The receiver performs full domain-schema validation. Different valid JSON serialization of an identical logical object under the same ID still conflicts because deduplication is byte-based.

## 5. Persistence, transactions, and invariants

[openInbox](../src/receiver.js) creates one STRICT SQLite table:

| Column | Role |
| --- | --- |
| event_id TEXT PRIMARY KEY | Durable event identity |
| digest TEXT NOT NULL | SHA-256 of raw body, independent of timestamp/signature |
| payload TEXT NOT NULL | Received JSON text |
| received_at TEXT NOT NULL | Receiver insertion time in ISO form |

A five-second busy timeout is set before WAL initialization. WAL is a write-ahead journal, not permission for concurrent writers. `record` starts BEGIN IMMEDIATE, reads the prior digest, returns a duplicate for identical content, rejects differing content with 409, or inserts the row. Commit and rollback surround that unit. SQLite is synchronous and blocks the Node event loop during work/lock waits.

The database owns durable duplicate recognition. Process counters do not: they reset when Node restarts. An event ID is globally unique within this inbox, not scoped by supplier. There is no migration/version framework, retention worker, payload encryption, processed flag, or outbox table.

Preserve these invariants:

1. Verify raw bytes and freshness before parsing/persisting trusted events.
2. An event ID may only refer to one raw-body digest.
3. A failed insertion leaves no event and does not consume the ID.
4. A duplicate is a successful delivery for the inbox, not a new business event.
5. The sender never changes event bytes to work around a conflict.
6. A matching acknowledgment is required before declaring delivery.
7. Retries stop at the attempt/server-delay budgets; no sleep follows the final attempt.

## 6. Retry algorithm and outcomes

[deliver](../sender/delivery.py) returns an immutable Delivery with event_id, delivered, attempts, status, duplicate, and reason. Each attempt signs using the current injected clock and calls the transport. The default transport uses urllib with a five-second socket timeout, refuses redirects, and reads at most 65,537 response bytes; oversized/invalid JSON becomes an empty payload and cannot be accepted as an acknowledgment.

On a retryable outcome, delay is `min(0.5 * 2 ** (attempt - 1), 10) + jitter * 0.25` seconds. A valid Retry-After, numeric seconds or HTTP date, overrides backoff. A past date gives zero delay. Invalid headers fall back to backoff. If the server asks for more than 60 seconds, the local harness stops with retry_after_exceeds_budget instead of retrying early. This is a per-delay budget, not a single total elapsed-time deadline. Attempts are limited to 1–8; default 4.

Reasons include accepted, permanent_http_error, retryable_http_error, network_error, invalid_acknowledgment, and retry_after_exceeds_budget. Read delivered and reason together: status 202 with invalid_acknowledgment is deliberately not success. The client keeps no persistent retry queue, so process termination loses unfinished scheduling state.

The transport, sleep, clock, and jitter functions are injectable. Tests can simulate failures deterministically without real delays. The complete demo separately proves Node and Python agree on actual wire signatures and persistence.

## 7. Configuration and authentication boundary

| Variable/option | Meaning |
| --- | --- |
| WEBHOOK_SECRET | Required shared random string, at least 32 characters |
| PORT | Receiver default 3001; 0 chooses an available port |
| DB_PATH | Default ./data/inbox.sqlite |
| DEMO_FAIL_FIRST | 0–10 temporary failures; default 0 |
| --url | Python target, default http://127.0.0.1:3001/webhooks/orders |
| --events | JSON array file; defaults examples/events.json |
| --attempts | 1–8, default 4 |

The CLI prints only delivery metadata, not secret/body. Exit 0 means all events delivered; 1 means at least one delivery failed; 2 means configuration/input was invalid. Events are processed sequentially, so a later bad input does not undo already delivered events. `.env` is ignored but is not automatically loaded by either executable.

To run separately, set the same generated secret in both terminals (see [README](../README.md)). Receiver listens only on loopback. GET /health is unauthenticated liveness. GET /stats requires `Authorization: Bearer <same development secret>` and returns process counters plus durable row count. There is no separate administrator identity. HMAC supplies authentication/integrity, not HTTPS encryption, secret rotation, or user authorization. Do not introduce real customer data into this lab.

## 8. Failure labs with exact expected outcomes

### A. Clock skew and changed bytes

```bash
node --test --test-name-pattern='signature authenticates' test/receiver.test.js
```

A valid signed body passes at the test clock. Changed body bytes, malformed signatures, timestamps 301 seconds old or in the future, and absent headers fail. In real use, check clocks and sign the exact bytes sent; do not widen the window to hide serialization errors.

### B. Concurrent duplicate delivery and ID conflict

```bash
node --test --test-name-pattern='HTTP receiver' test/receiver.test.js
```

After one deliberate validated 503, eight concurrent same-event HTTP requests produce exactly one 202 and seven 200 responses. Stored count is one. A valid signed changed body with the same ID returns 409 and is not another saved event. Bad signatures/schema/body sizes fail before the simulated failure or attempt counter.

### C. Database failure and retry

```bash
node --test --test-name-pattern='database insert failure' test/receiver.test.js
```

A temporary SQLite trigger aborts insertion. Expected stored count is zero. Dropping the trigger permits the original ID/body to insert, then replay as a duplicate, leaving exactly one row. The separate reopen test proves duplicates remain recognizable after closing and reopening the file.

### D. A false 2xx cannot confirm delivery

```bash
python3 -m unittest discover -s tests -p test_sender.py -v
```

The acknowledgment tests provide an empty 200, another event ID, a string instead of a boolean, inconsistent 200/duplicate=false, and empty 204. Each retries once under a two-attempt budget and returns delivered=false with invalid_acknowledgment. Another case simulates a lost acknowledgment followed by a matching duplicate: expect delivered=true after two attempts and identical request bytes. This models uncertainty without pretending the first write was undone.

### E. Backoff, Retry-After, and permanent errors

The Python suite simulates 503/Retry-After=2, then 429 without a header, then success. With jitter fixed to zero, sleeps are exactly 2 and 1 seconds. A long 120-second Retry-After stops immediately without sleeping or shortening it. A network failure with three attempts sleeps 0.5 then 1 and stops. Permanent statuses including 409 and 302 never sleep or retry. These are policy checks, not real network-performance measurements.

### F. Cross-language proof and diagnosis

Run `python3 scripts/demo.py`. Expect the seven-attempt/two-row summary described above. On missing readiness, inspect the Node executable/version and stderr. On 401, check shared secret, clock freshness, and raw-byte serialization without printing the secret. On 409, compare event identity and original bytes: do not blindly mint a new ID to bypass duplicate protection. If state appears missing after restart, inspect DB_PATH and working directory. Counters resetting while rows persist is expected.

## 9. Extension exercises and solution directions

The original [EXERCISES.md](../EXERCISES.md) offers three levels.

1. **A second event type:** add a separate schema branch for order.cancelled with its own fields, while retaining order.created checks. Test both languages and conflicting IDs. Do not relax all event validation to accommodate one new type.
2. **Persistent sender queue:** store event ID, exact encoded bytes, attempt count, next eligible time, and outcome in SQLite. Resume after restart with fresh signatures and unchanged body. Handle worker ownership atomically so two workers do not both retry one job indefinitely. Keep Retry-After policy explicit.
3. **Downstream worker/outbox:** write processing state and an outbound action record in one database transaction, then deliver the action with its own destination idempotency contract. Test a crash before/after commit and an ambiguous external timeout. A processed flag followed by an untracked network call loses work or repeats effects.
4. **Secret rotation:** choose a key-ID/header scheme, bounded overlapping verification window, and an administrative boundary separate from /stats. Test old/new/unknown keys and avoid logging key material. HMAC key rotation is not end-user login.

## 10. Interview questions with honest answers

**Why sign raw bytes?** Parsing and reserializing can change whitespace/key order. Both parties must authenticate exactly the transmitted bytes.

**Why a timestamp as well as HMAC?** A captured valid message remains signed forever unless freshness is checked. A valid current duplicate still succeeds via durable inbox identity.

**Why not treat every 200 as success?** A proxy or wrong endpoint can return 200 without acknowledging the event. A validated event-specific acknowledgment distinguishes transport success from the receiver's promised result.

**Does this guarantee exactly once?** Only one stored inbox row per ID/body. Downstream side effects have no implementation or guarantee; they need a separate transactional and idempotency strategy.

**Why backoff and jitter?** Backoff reduces pressure during outages; jitter spreads clients across time. Neither guarantees eventual success, so the client has explicit budgets and failure outcomes.

**What do tests establish?** Local signature compatibility, validation, rollback, duplicate persistence, concurrent HTTP handling, retry classification, and a real Python→Node→SQLite demonstration. They do not establish production throughput, real-provider compatibility, or a durable client queue.

No lint/type/build/browser tools are configured: Node and Python source execute directly and this project has no UI. Keep both runtimes patched; no third-party packages does not remove runtime/security obligations. The workflow verifies only; it does not deploy anything.
