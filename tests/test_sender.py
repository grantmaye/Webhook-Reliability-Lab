import hashlib
import hmac
import unittest
import urllib.error

from sender.delivery import deliver, signed_headers, retry_after_seconds

EVENT = {"id": "evt_1", "type": "order.created", "data": {"order_id": "ORD1", "amount_cents": 12999, "currency": "USD"}}
SECRET = "development-only-secret-long-enough-for-tests"
URL = "http://127.0.0.1:3001/webhooks/orders"


class SenderTests(unittest.TestCase):
    def test_signature_matches_hmac_standard(self):
        headers = signed_headers(SECRET, b'{"id":"1"}', 1800000000)
        expected = hmac.new(SECRET.encode(), b'1800000000.{"id":"1"}', hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Webhook-Signature"], expected)

    def test_retries_transient_statuses_honors_retry_after_and_keeps_bytes(self):
        calls, sleeps = [], []
        replies = iter([(503, {"Retry-After": "2"}, {}), (429, {}, {}),
                        (202, {}, {"event_id": EVENT["id"], "duplicate": False})])
        def transport(url, body, headers):
            calls.append((body, headers))
            return next(replies)
        times = iter([1800000000, 1800000001, 1800000002, 1800000003, 1800000004])
        result = deliver(URL, EVENT, SECRET, transport=transport, sleep=sleeps.append,
                         clock=lambda: next(times), jitter=lambda: 0)
        self.assertTrue(result.delivered)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(sleeps, [2, 1])
        self.assertEqual(calls[0][0], calls[2][0])
        self.assertNotEqual(calls[0][1]["X-Webhook-Timestamp"], calls[2][1]["X-Webhook-Timestamp"])

    def test_permanent_errors_never_retry(self):
        for status in [400, 401, 403, 409, 422, 302]:
            with self.subTest(status=status):
                result = deliver(URL, EVENT, SECRET, transport=lambda *_: (status, {}, {}),
                                 sleep=lambda _: self.fail("Should not sleep"))
                self.assertFalse(result.delivered)
                self.assertEqual(result.attempts, 1)

    def test_network_errors_stop_at_attempt_budget(self):
        sleeps = []
        def broken(*_):
            raise urllib.error.URLError("simulated timeout")
        result = deliver(URL, EVENT, SECRET, transport=broken, sleep=sleeps.append, max_attempts=3, jitter=lambda: 0)
        self.assertFalse(result.delivered)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.reason, "network_error")
        self.assertEqual(sleeps, [0.5, 1])

    def test_long_retry_after_is_not_shortened(self):
        result = deliver(URL, EVENT, SECRET, transport=lambda *_: (429, {"Retry-After": "120"}, {}),
                         sleep=lambda _: self.fail("Should stop within budget"))
        self.assertEqual(result.reason, "retry_after_exceeds_budget")
        self.assertEqual(result.attempts, 1)

    def test_http_date_and_invalid_retry_after(self):
        self.assertEqual(retry_after_seconds({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, 1445412470), 10)
        self.assertIsNone(retry_after_seconds({"retry-after": "bad-value"}, 0))

    def test_duplicate_acknowledgment_is_success(self):
        result = deliver(URL, EVENT, SECRET, transport=lambda *_: (200, {}, {"event_id": EVENT["id"], "duplicate": True}))
        self.assertTrue(result.delivered)
        self.assertTrue(result.duplicate)

    def test_invalid_success_acknowledgments_never_claim_delivery(self):
        for status, payload in [(200, {}), (202, {"event_id": "other", "duplicate": False}),
                                (202, {"event_id": EVENT["id"], "duplicate": "false"}),
                                (200, {"event_id": EVENT["id"], "duplicate": False}), (204, {})]:
            with self.subTest(status=status, payload=payload):
                sleeps = []
                result = deliver(URL, EVENT, SECRET, max_attempts=2,
                                 transport=lambda *_: (status, {}, payload), sleep=sleeps.append, jitter=lambda: 0)
                self.assertFalse(result.delivered)
                self.assertEqual(result.reason, "invalid_acknowledgment")
                self.assertEqual(result.attempts, 2)
                self.assertEqual(sleeps, [0.5])

    def test_unknown_outcome_recovers_with_a_matching_duplicate_acknowledgment(self):
        calls = []
        def transport(_url, body, _headers):
            calls.append(body)
            if len(calls) == 1:
                return 202, {}, {}  # Receiver may have committed; acknowledgment was lost.
            return 200, {}, {"event_id": EVENT["id"], "duplicate": True}
        result = deliver(URL, EVENT, SECRET, transport=transport, sleep=lambda _: None)
        self.assertTrue(result.delivered)
        self.assertTrue(result.duplicate)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(calls[0], calls[1])

    def test_rejects_bad_configuration(self):
        for url in ["file:///etc/passwd", "http://user:secret@localhost/test", "https://example.test/#fragment"]:
            with self.assertRaises(ValueError):
                deliver(url, EVENT, SECRET)
        with self.assertRaises(ValueError):
            deliver(URL, EVENT, SECRET, max_attempts=0)
        with self.assertRaises(ValueError):
            deliver(URL, EVENT, "short")


if __name__ == "__main__":
    unittest.main()
