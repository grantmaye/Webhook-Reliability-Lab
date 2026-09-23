# Make the project your own

Run `python3 scripts/demo.py`, then follow the same event through signing, HTTP delivery, verification and persistence.

## Small contribution

Add a second event type, such as `order.cancelled`, with its own required fields. Test the contract in both languages without weakening validation for `order.created`.

## More substantial contribution

Persist failed client deliveries in a SQLite queue. Store the event ID, payload, attempt count and next eligible attempt time. Add a command that resumes after a restart. Use the existing event ID on every retry.

## Advanced contribution

Add a worker that processes received events. Design a transaction/outbox pattern that avoids losing work between marking an event processed and sending an external action. Describe precisely what is and is not guaranteed when the external destination times out.

## Be ready to explain

1. Why sign the raw bytes instead of parsing and reserializing JSON first?
2. Why include a timestamp if there is already an HMAC signature?
3. Why is a duplicate acknowledgment a successful delivery?
4. What happens if the receiver commits and the response is lost?
5. How do backoff and jitter help during an outage?
6. Why does unique event storage not guarantee exactly-once business actions?

Explain the flow in your own words and show one extension you implemented and tested. This new personal repository does not establish that a prior employer used these languages.
