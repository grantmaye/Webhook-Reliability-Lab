"""Start Node, retry two injected failures, replay deliveries, then verify SQLite."""
import json
import os
import queue
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sender.delivery import deliver, as_json  # noqa: E402


def main():
    node = shutil.which("node")
    if not node:
        raise SystemExit("Install Node.js 24 and put node on PATH.")
    secret = secrets.token_hex(32)
    with tempfile.TemporaryDirectory() as folder:
        filename = str(Path(folder) / "inbox.sqlite")
        env = {**os.environ, "WEBHOOK_SECRET": secret, "PORT": "0", "DB_PATH": filename, "DEMO_FAIL_FIRST": "2"}
        child = subprocess.Popen([node, "src/server.js"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            ready = queue.Queue()
            threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
            base = json.loads(ready.get(timeout=10))["url"]
            events = json.loads((ROOT / "examples/events.json").read_text())
            first = deliver(base + "/webhooks/orders", events[0], secret)
            print(as_json(first))
            if not first.delivered or first.attempts != 3:
                raise RuntimeError("Expected success after two injected transient failures")
            second = deliver(base + "/webhooks/orders", events[1], secret)
            print(as_json(second))
            if not second.delivered:
                raise RuntimeError("Second event was not accepted")
            for event in events:
                duplicate = deliver(base + "/webhooks/orders", event, secret)
                print(as_json(duplicate))
                if not duplicate.delivered or not duplicate.duplicate:
                    raise RuntimeError("Replay did not produce a duplicate acknowledgment")
            altered = json.loads(json.dumps(events[0])); altered["data"]["amount_cents"] = 1
            conflict = deliver(base + "/webhooks/orders", altered, secret)
            print(as_json(conflict))
            if conflict.status != 409 or conflict.attempts != 1:
                raise RuntimeError("Conflicting event should fail without a retry")
            request = urllib.request.Request(base + "/stats", headers={"Authorization": f"Bearer {secret}"})
            with urllib.request.urlopen(request, timeout=5) as response:
                stats = json.load(response)
            with sqlite3.connect(filename) as db:
                actual_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            if stats["stored_events"] != 2 or actual_count != 2:
                raise RuntimeError("Expected exactly two persisted events")
            print(json.dumps({"verified": True, "stats": stats}, indent=2))
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()
            errors = child.stderr.read()
            child.stdout.close(); child.stderr.close()
            if child.returncode not in (0, -15) and errors:
                print(errors, file=sys.stderr)


if __name__ == "__main__":
    main()
