#!/usr/bin/env python3
# NOTE: On Nix servers, do NOT run directly. Use: /tmp/python3 /tmp/link_server.py
"""HTTP server on port 8080: tmate links + alarm mesh API + remote exec."""
import http.server
import json
import os
import time
import random
import hashlib
import threading
import subprocess
import base64
from pathlib import Path

LEDGER_PATH = Path("/tmp/visit_ledger.json")
STATE_PATH = Path("/tmp/alarm_state.json")
EXEC_SECRET = os.environ.get("EXEC_SECRET", "vps123-exec-key")
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
            # Check local services health
            local_health = {}
            for name, port in [("code-oss", 80), ("admin-panel", 8081)]:
                try:
                    import socket
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(2)
                    s.connect(("127.0.0.1", port))
                    s.close()
                    local_health[name] = "up"
                except Exception:
                    local_health[name] = "down"
            # Check tmate
            try:
                r = subprocess.run(["pgrep", "-f", "tmate.*-F"],
                                   capture_output=True, timeout=5)
                local_health["tmate"] = "up" if r.returncode == 0 else "down"
            except Exception:
                local_health["tmate"] = "unknown"
            # Check alarm_mesh
            try:
                r = subprocess.run(["pgrep", "-f", "alarm_mesh.py"],
                                   capture_output=True, timeout=5)
                local_health["alarm_mesh"] = "up" if r.returncode == 0 else "down"
            except Exception:
                local_health["alarm_mesh"] = "unknown"
            self._json_response(200, {
                "server_uptime": int(time.time() - START_TIME),
                "alarm_state": alarm_state,
                "ledger_entries": len(load_ledger()),
                "local_health": local_health,
            })

        elif self.path.startswith("/logs"):
            # /logs — returns recent alarm_mesh.log lines
            # /logs?file=watchdog&lines=50 — specific log file
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            fname = params.get("file", ["alarm_mesh"])[0]
            n_lines = min(int(params.get("lines", ["100"])[0]), 500)
            allowed_logs = {
                "alarm_mesh": "/tmp/alarm_mesh.log",
                "watchdog": "/tmp/watchdog.log",
                "start": "/tmp/start.log",
                "tmate": "/tmp/tmate.log",
                "keepalive": "/tmp/keepalive.log",
                "link_server": "/tmp/link_server.log",
            }
            log_path = allowed_logs.get(fname)
            if not log_path:
                self._json_response(400, {"error": f"unknown log: {fname}", "available": list(allowed_logs.keys())})
                return
            try:
                with open(log_path) as f:
                    lines = f.readlines()
                tail = lines[-n_lines:]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write("".join(tail).encode())
            except FileNotFoundError:
                self._json_response(404, {"error": f"log not found: {log_path}"})
            except Exception as e:
                self._json_response(500, {"error": str(e)})

        elif self.path.startswith("/events"):
            # /events — returns recent structured events from alarm_events.jsonl
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            n_lines = min(int(params.get("lines", ["50"])[0]), 200)
            events_path = "/tmp/alarm_events.jsonl"
            try:
                with open(events_path) as f:
                    lines = f.readlines()
                tail = lines[-n_lines:]
                events = []
                for line in tail:
                    try:
                        events.append(json.loads(line.strip()))
                    except Exception:
                        pass
                self._json_response(200, {"events": events, "total": len(lines)})
            except FileNotFoundError:
                self._json_response(200, {"events": [], "total": 0})
            except Exception as e:
                self._json_response(500, {"error": str(e)})

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

        elif self.path == "/exec":
            try:
                data = self._read_body()
            except Exception:
                self._json_response(400, {"error": "bad request"})
                return
            secret = str(data.get("secret", "")).strip()
            if secret != EXEC_SECRET:
                self._json_response(403, {"error": "forbidden"})
                return
            script = str(data.get("script", "")).strip()
            b64script = str(data.get("b64", "")).strip()
            timeout_s = min(int(data.get("timeout", 30)), 120)
            if b64script:
                try:
                    script = base64.b64decode(b64script).decode("utf-8")
                except Exception:
                    self._json_response(400, {"error": "bad base64"})
                    return
            if not script:
                self._json_response(400, {"error": "no script"})
                return
            try:
                exec_cwd = "/home/user/vps123" if os.path.isdir("/home/user/vps123") else os.path.expanduser("~")
                result = subprocess.run(
                    ["/bin/bash", "-c", script],
                    capture_output=True, text=True,
                    timeout=timeout_s,
                    cwd=exec_cwd,
                    env={
                        **os.environ,
                        "PATH": "/tmp:/usr/local/bin:/usr/bin:/bin:/home/user/.global_modules/bin:" + os.environ.get("PATH", ""),
                    },
                )
                self._json_response(200, {
                    "exit_code": result.returncode,
                    "stdout": result.stdout[-8000:],
                    "stderr": result.stderr[-2000:],
                })
            except subprocess.TimeoutExpired:
                self._json_response(200, {"exit_code": -1, "stdout": "", "stderr": "timeout"})
            except Exception as e:
                self._json_response(500, {"error": str(e)})

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 8080), Handler)
    server.request_queue_size = 20
    print("[link-server] Listening on port 8080 (links + alarm API + exec + logs)")
    server.serve_forever()
