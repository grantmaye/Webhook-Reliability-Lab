from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime

RETRYABLE = {408, 429, 500, 502, 503, 504}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Delivery:
    event_id: str
    delivered: bool
    attempts: int
    status: int | None
    duplicate: bool
    reason: str


def signed_headers(secret: str, body: bytes, timestamp: int) -> dict[str, str]:
    stamp = str(timestamp)
    signature = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Webhook-Timestamp": stamp, "X-Webhook-Signature": signature}


def send_http(url: str, body: bytes, headers: dict) -> tuple[int, dict, dict]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        response = urllib.request.build_opener(NoRedirect()).open(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(65537)
        try:
            parsed = json.loads(raw) if len(raw) <= 65536 else {}
        except (ValueError, UnicodeDecodeError):
            parsed = {}
        return response.code, dict(response.headers), parsed if isinstance(parsed, dict) else {}


def retry_after_seconds(headers: dict, now: float) -> float | None:
    value = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        if str(value).isdigit():
            return float(value)
        return max(0.0, parsedate_to_datetime(value).timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return None


def deliver(url: str, event: dict, secret: str, *, max_attempts: int = 4,
            transport=send_http, sleep=time.sleep, clock=time.time, jitter=random.random) -> Delivery:
    target = urllib.parse.urlsplit(url)
    if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password or target.fragment:
        raise ValueError("Provide an HTTP(S) endpoint without credentials or fragments")
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValueError("Secret must contain at least 32 characters")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 8:
        raise ValueError("max_attempts must be 1 to 8")
    if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]:
        raise ValueError("Event must have a nonempty string id")
    # Keep the exact event bytes stable on retries, but refresh the signed timestamp.
    body = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > 65536:
        raise ValueError("Event exceeds the receiver's 64 KiB limit")
    status = None
    reason = "attempts_exhausted"
    for attempt in range(1, max_attempts + 1):
        response_headers = {}
        try:
            status, response_headers, payload = transport(url, body, signed_headers(secret, body, int(clock())))
            if 200 <= status < 300:
                # A proxy/error page can return 2xx without acknowledging this
                # event. Treat that outcome as unknown and retry the same bytes.
                if (isinstance(payload, dict) and payload.get("event_id") == event["id"]
                        and type(payload.get("duplicate")) is bool
                        and ((status == 202 and payload["duplicate"] is False)
                             or (status == 200 and payload["duplicate"] is True))):
                    return Delivery(event["id"], True, attempt, status, payload["duplicate"], "accepted")
                reason = "invalid_acknowledgment"
            elif status not in RETRYABLE:
                return Delivery(event["id"], False, attempt, status, False, "permanent_http_error")
            else:
                reason = "retryable_http_error"
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            status = None
            reason = "network_error"
        if attempt < max_attempts:
            delay = retry_after_seconds(response_headers, clock())
            if delay is None:
                delay = min(0.5 * 2 ** (attempt - 1), 10) + jitter() * 0.25
            # Do not violate a long server Retry-After by retrying early.
            if delay > 60:
                return Delivery(event["id"], False, attempt, status, False, "retry_after_exceeds_budget")
            sleep(delay)
    return Delivery(event["id"], False, max_attempts, status, False, reason)


def as_json(delivery: Delivery) -> str:
    return json.dumps(asdict(delivery), sort_keys=True)
