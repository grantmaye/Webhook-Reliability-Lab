# Why a webhook reliability lab matters

## The problem behind the software

An HTTP timeout is ambiguous. A receiver may have saved an event before its response disappeared, or may never have received it. A sender that never retries can lose work; a sender that blindly retries can duplicate effects. Even an HTTP 200 may come from the wrong endpoint or an intermediary rather than a meaningful acknowledgment.

Webhook Reliability Lab is an independent personal portfolio project built around synthetic orders. There is no real provider account, customer order stream, historical outage, revenue claim, or former employer implementation in this story. Its backstory is an engineering question made runnable: how can two small programs handle uncertain delivery honestly?

## A hypothetical integration incident

Imagine a fictional shop sending order-created events to a warehouse integration. During a brief receiver outage, requests fail. Before a deliberate delivery policy, an operator might rerun an export manually, worry about duplicate orders, and inspect logs that disagree with database state.

In this lab, Python signs an order event and sends it to Node. Node rejects bad signatures and stale timestamps, validates the event, and persists the inbox record. When the receiver deliberately returns 503 twice, Python waits and retries the same event bytes with fresh signatures. The third attempt is accepted. A later replay receives a duplicate acknowledgment and counts as successful delivery without inserting another row.

If the same ID arrives with changed content, the receiver returns 409. If a success response lacks a valid acknowledgment for the correct event ID, the sender keeps the outcome uncertain and retries within its budget. These choices make failure behavior inspectable; they do not claim that a real shop's fulfillment has been completed.

## Who benefits and why

Integration developers can study the boundary between transport delivery, authentication, duplicate recognition, and business processing. Backend reviewers can follow both sides of an actual HTTP exchange and independently inspect the saved SQLite rows. Python learners can see typed results, injected test dependencies, retry decisions, and subprocess orchestration. An engineering team evaluating a webhook design can use the lab to ask what should happen after a timeout before connecting a real system.

The [demo](../scripts/demo.py), [receiver](../src/receiver.js), and [sender](../sender/delivery.py) provide the evidence. The value is a reproducible failure conversation and clear outcomes, not a claim of production reliability metrics.

| Situation | Hypothetical improvised workflow | Demonstrated behavior |
| --- | --- | --- |
| Temporary outage | Manual rerun or immediate repeated requests | Bounded exponential backoff/jitter and Retry-After |
| Captured request | Trust arbitrary JSON | Raw-body HMAC plus freshness check |
| Lost response | Guess whether processing happened | Retry stable bytes and accept a durable duplicate acknowledgment |
| Same ID, changed data | Silently overwrite an event | Explicit conflict and no retry |
| Misleading 200 | Declare success from status alone | Require event-specific acknowledgment |
| Restart | Forget duplicate state in memory | Reopen the durable inbox |
| Business side effects | Assume one row means exactly once | State that downstream processing still needs its own design |

## A 60–90 second demonstration

“This lab has two real programs: a Python delivery client and a Node webhook receiver. The orders and secret are temporary demo data.

I start the complete demo. The receiver deliberately fails the first two validated requests with 503. Python respects the requested delay and succeeds on attempt three. It signs a fresh timestamp each time but keeps the event bytes unchanged.

Now a second event is accepted. Replaying both returns duplicate acknowledgments, so the sender can stop successfully while the database still contains only two rows.

I then reuse an event ID with changed content. The receiver returns 409 and the client does not retry a permanent conflict.

The client also checks acknowledgments: a bare 200 from an intermediary is not proof that this event was saved. Tests cover missing or mismatched responses and recovery through a matching duplicate acknowledgment.

Finally, the demo reads both the diagnostic endpoint and SQLite directly. They agree on two saved events.

This is an idempotent inbox, not exactly-once fulfillment. A downstream worker needs its own transaction and outbound-action strategy.”

## Honest limits and the next conversation

The receiver binds to localhost. HMAC protects integrity/authenticity but does not encrypt HTTP traffic. The diagnostic endpoint reuses a development secret, not a separate administrator identity. Attempt counters reset on restart, while event rows survive. There is no persistent client queue, rate limiter, retention, secret rotation, production TLS setup, or provider-specific signing adapter. SQLite operations block the Node event loop.

A real integration would define ownership of event IDs, byte/signature conventions, retry deadlines, authentication, retention, and downstream actions with both parties. The [technical manual](technical-manual.md) teaches those boundaries through exact contracts, failure labs, and extension solutions. It gives a maintainer a basis for deciding what to build next without pretending those features already exist.
