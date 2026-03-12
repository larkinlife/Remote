#!/usr/bin/env python3
"""HTTP server on port 8080: tmate links + alarm mesh API."""
import http.server
import json
import os
import time
import random
import hashlib
import threading
from pathlib import Path

LEDGER_PATH = Path("/tmp/visit_ledger.json")
STATE_PATH = Path("/tmp/alarm_state.json")
START_TIME = time.time()
_ledger_lock = threading.Lock()

_RESPONSES = [
    "ack", "ok", "roger", "copy", "noted", "received", "confirmed",
    "pong", "yes", "alive", "here", "ready", "standing-by",
    "affirmative", "present", "online", "active", "good",
]


def _nonce():
    return hashlib.md5(os.urandom(16)).hexdigest()[:12]


def load_ledger():
    with _ledger_lock:
        try:
            return json.loads(LEDGER_PATH.read_text())
        except Exception:
            return {}


def save_ledger(ledger):
    with _ledger_lock:
        LEDGER_PATH.write_text(json.dumps(ledger, indent=2))


def merge_ledgers(local, remote):
    merged = dict(local)
    for key, ts in remote.items():
        if key not in merged or ts > merged[key]:
            merged[key] = ts
    return merged


class Handler(http.server.BaseHTTPRequestHandler):
    def _json_response(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            return json.loads(self.rfile.read(length))
        return {}

    def do_GET(self):
        if self.path in ("/", "/links"):
            try:
                with open("/tmp/tmate_links.txt") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(content.encode())
            except FileNotFoundError:
                self.send_response(503)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"tmate not ready yet\n")

        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")

        elif self.path == "/ledger":
            self._json_response(200, load_ledger())

        elif self.path == "/status":
            try:
                alarm_state = json.loads(STATE_PATH.read_text())
            except Exception:
                alarm_state = {}
            self._json_response(200, {
                "server_uptime": int(time.time() - START_TIME),
                "alarm_state": alarm_state,
                "ledger_entries": len(load_ledger()),
            })

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/heartbeat":
            try:
                data = self._read_body()
            except Exception:
                data = {}

            if "ledger" in data and isinstance(data["ledger"], dict):
                local = load_ledger()
                merged = merge_ledgers(local, data["ledger"])
                save_ledger(merged)

            from datetime import datetime, timezone
            resp = {
                "status": random.choice(_RESPONSES),
                "nonce": _nonce(),
                "ts": datetime.now(timezone.utc).isoformat(),
                "from": os.environ.get("ALARM_SELF_ID", os.environ.get("WEB_HOST", "unknown")),
                "uptime": int(time.time() - START_TIME),
                "load": round(random.uniform(0.1, 3.0), 2),
                "ledger": load_ledger(),
            }
            self._json_response(200, resp)

        elif self.path == "/ledger":
            try:
                incoming = self._read_body()
            except Exception:
                incoming = {}
            if isinstance(incoming, dict):
                local = load_ledger()
                merged = merge_ledgers(local, incoming)
                save_ledger(merged)
            self._json_response(200, {"merged": True, "entries": len(load_ledger())})

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 8080), Handler)
    server.request_queue_size = 20
    print("[link-server] Listening on port 8080 (links + alarm API)")
    server.serve_forever()
