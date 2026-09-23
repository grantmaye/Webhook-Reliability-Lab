import argparse
import json
import os
import sys
from pathlib import Path
from .delivery import as_json, deliver


def main(argv=None):
    parser = argparse.ArgumentParser(description="Deliver signed demo events with bounded retries")
    parser.add_argument("--events", type=Path, default=Path("examples/events.json"))
    parser.add_argument("--url", default="http://127.0.0.1:3001/webhooks/orders")
    parser.add_argument("--attempts", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        events = json.loads(args.events.read_text(encoding="utf-8"))
        if not isinstance(events, list) or not events:
            raise ValueError("Input must be a nonempty array of events")
        secret = os.environ.get("WEBHOOK_SECRET", "")
        failed = False
        for event in events:
            result = deliver(args.url, event, secret, max_attempts=args.attempts)
            print(as_json(result))
            failed = failed or not result.delivered
        return 1 if failed else 0
    except (ValueError, OSError) as error:
        print(f"sender: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
