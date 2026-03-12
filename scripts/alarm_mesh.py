#!/usr/bin/env python3
# NOTE: On Nix servers, do NOT run directly. Use: /tmp/python3 /tmp/alarm_mesh.py
"""
Alarm Mesh — Firebase Studio cross-wake system.

Keeps N Firebase Studio workspaces alive through:
- Full mesh heartbeats (all-to-all, random intervals 30-90s)
- Wake-up visits with retry for downed machines (3m/5m/10m backoff)
- Shared visit ledger to avoid duplicate wake-ups
- Survivor mode: if 2+ peers down, aggressive wake (every 5min)
- Independent: each machine makes own decisions, no single point of failure

Machine registry loaded from machines.json (not hardcoded).
"""

import json, os, sys, time, random, hashlib, threading, logging, subprocess
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# ===================================================================
# CONFIG LOADING — machines.json is the single source of truth
# ===================================================================
_CONFIG_SEARCH_PATHS = [
    Path("/tmp/machines.json"),
    Path(__file__).parent / "../config/machines.json",
    Path(__file__).parent / "machines.json",
    Path("/home/user") / "vps123/config/machines.json",
    Path("/home/user") / "Remote/config/machines.json",
]


def load_machines_config():
    """Find and load machines.json. Returns dict {id: machine_data}."""
    for p in _CONFIG_SEARCH_PATHS:
        try:
            p = p.resolve()
            if p.exists():
                data = json.loads(p.read_text())
                machines = {}
                for m in data.get("machines", []):
                    machines[m["id"]] = {
                        "workspace": m["workspace"],
                        "web_host": m["web_host"],
                        "account": m["account"],
                        "project": m.get("project", ""),
                    }
                if machines:
                    return machines, str(p)
        except Exception:
            continue
    return {}, ""


MACHINES, _config_path = load_machines_config()

# ===================================================================
# TIMING CONSTANTS
# ===================================================================
HEARTBEAT_MIN_S = 30
HEARTBEAT_MAX_S = 90
VISIT_INTERVAL_S = 7200       # 2h — normal wake-visit interval
VISIT_GRACE_S = 5400          # 90min — skip if someone visited recently
WAKE_RETRY_DELAYS = [180, 300, 600]  # 3m, 5m, 10m
WAKE_COOLDOWN_S = 1200        # 20min between wake attempts for same target
HEARTBEAT_FAIL_THRESHOLD = 5  # consecutive fails → emergency wake
SURVIVOR_THRESHOLD = 2        # 2+ peers dead → survivor mode
SURVIVOR_WAKE_S = 300         # 5min wake interval in survivor mode
SURVIVOR_HEARTBEAT_S = 20     # fast heartbeats in survivor mode

# ===================================================================
# PATHS
# ===================================================================
LEDGER_PATH = Path("/tmp/visit_ledger.json")
STATE_PATH = Path("/tmp/alarm_state.json")
LOG_PATH = Path("/tmp/alarm_mesh.log")

# ===================================================================
# SELF DETECTION
# ===================================================================
SELF_ID = os.environ.get("ALARM_SELF_ID", "")
if not SELF_ID:
    try:
        SELF_ID = Path("/tmp/alarm_self_id").read_text().strip()
    except Exception:
        pass
if not SELF_ID:
    my_host = os.environ.get("WEB_HOST", "")
    for mid, m in MACHINES.items():
        if m["web_host"] == my_host:
            SELF_ID = mid
            break
if not SELF_ID:
    try:
        hn = open("/proc/sys/kernel/hostname").read().strip()
        for mid, m in MACHINES.items():
            if m["workspace"] in hn or hn in m["workspace"]:
                SELF_ID = mid
                break
    except Exception:
        pass

PEER_IDS = [pid for pid in MACHINES if pid != SELF_ID]

# ===================================================================
# LOGGING
# ===================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("alarm")

START_TIME = time.time()

# ===================================================================
# RANDOM PAYLOADS — varied to look organic
# ===================================================================
_WORDS = [
    "check", "ping", "sync", "hello", "status", "verify", "pulse",
    "wave", "knock", "probe", "scan", "test", "beacon", "signal",
    "heartbeat", "alive", "health", "monitor", "watch", "guard",
    "update", "refresh", "poll", "query", "report", "log", "trace",
]
_ADJS = [
    "quick", "lazy", "warm", "cold", "fresh", "deep", "light",
    "dark", "calm", "wild", "soft", "loud", "fast", "slow",
    "blue", "red", "green", "sharp", "smooth", "round", "flat",
]
_RESPONSES = [
    "ack", "ok", "roger", "copy", "noted", "received", "confirmed",
    "pong", "yes", "alive", "here", "ready", "standing-by",
    "affirmative", "present", "online", "active", "good",
]


def _nonce():
    return hashlib.md5(os.urandom(16)).hexdigest()[:12]


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _now_ts():
    return time.time()


def random_payload():
    return {
        "action": random.choice(_WORDS),
        "tag": f"{random.choice(_ADJS)}-{random.choice(_WORDS)}-{random.randint(10,99)}",
        "nonce": _nonce(),
        "ts": _now_iso(),
        "from": SELF_ID,
        "seq": random.randint(1000, 99999),
        "v": random.choice(["1.0", "1.1", "2.0"]),
    }


def random_response():
    return {
        "status": random.choice(_RESPONSES),
        "nonce": _nonce(),
        "ts": _now_iso(),
        "from": SELF_ID,
        "uptime": int(time.time() - START_TIME),
        "load": round(random.uniform(0.1, 2.5), 2),
    }


# ===================================================================
# LEDGER — shared visit log to coordinate wake-ups
# ===================================================================
_ledger_lock = threading.Lock()


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


def record_visit(visitor_id, target_id):
    ledger = load_ledger()
    ledger[f"{visitor_id}>{target_id}"] = _now_iso()
    save_ledger(ledger)


def was_recently_visited(target_id, grace_s=VISIT_GRACE_S):
    ledger = load_ledger()
    now = _now_ts()
    for key, ts_str in ledger.items():
        if key.endswith(f">{target_id}"):
            try:
                visit_ts = datetime.fromisoformat(ts_str).timestamp()
                if now - visit_ts < grace_s:
                    return True
            except Exception:
                pass
    return False


def cleanup_ledger(max_age_s=86400):
    ledger = load_ledger()
    now = _now_ts()
    cleaned = {}
    for key, ts_str in ledger.items():
        try:
            visit_ts = datetime.fromisoformat(ts_str).timestamp()
            if now - visit_ts < max_age_s:
                cleaned[key] = ts_str
        except Exception:
            pass
    save_ledger(cleaned)


# ===================================================================
# PEER STATE — track health of each peer
# ===================================================================
_state_lock = threading.Lock()


class PeerState:
    def __init__(self):
        self.peers = {}
        self._load()

    def _load(self):
        with _state_lock:
            try:
                data = json.loads(STATE_PATH.read_text())
                self.peers = data.get("peers", {})
            except Exception:
                self.peers = {}

    def _save(self):
        with _state_lock:
            STATE_PATH.write_text(json.dumps({
                "self": SELF_ID,
                "peers": self.peers,
                "updated": _now_iso(),
                "uptime": int(time.time() - START_TIME),
            }, indent=2))

    def mark_alive(self, pid):
        if pid not in self.peers:
            self.peers[pid] = {}
        self.peers[pid].update({
            "alive": True,
            "last_seen": _now_ts(),
            "consecutive_fails": 0,
            "wake_attempts": 0,
        })
        self._save()

    def mark_fail(self, pid):
        if pid not in self.peers:
            self.peers[pid] = {"alive": None, "last_seen": 0, "wake_attempts": 0, "last_wake": 0}
        self.peers[pid]["consecutive_fails"] = self.peers[pid].get("consecutive_fails", 0) + 1
        self.peers[pid]["alive"] = False
        self._save()

    def get(self, pid):
        return self.peers.get(pid, {
            "alive": None, "last_seen": 0, "consecutive_fails": 0,
            "wake_attempts": 0, "last_wake": 0,
        })

    def record_wake(self, pid):
        if pid not in self.peers:
            self.peers[pid] = {}
        self.peers[pid]["wake_attempts"] = self.peers[pid].get("wake_attempts", 0) + 1
        self.peers[pid]["last_wake"] = _now_ts()
        self._save()

    def count_dead(self):
        dead = 0
        for pid in PEER_IDS:
            ps = self.get(pid)
            if ps.get("alive") is False:
                dead += 1
            elif ps.get("alive") is None and ps.get("last_seen", 0) == 0:
                dead += 1
        return dead

    def is_survivor_mode(self):
        return self.count_dead() >= SURVIVOR_THRESHOLD


state = PeerState()

# ===================================================================
# GCLOUD AUTH
# ===================================================================
_available_accounts = set()


def detect_accounts():
    global _available_accounts
    try:
        r = subprocess.run(
            ["gcloud", "auth", "list", "--format=value(account)", "--filter=status:ACTIVE"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().split("\n"):
                acc = line.strip()
                if acc:
                    _available_accounts.add(acc)
    except Exception as e:
        log.warning(f"gcloud auth list failed: {e}")

    for m in MACHINES.values():
        acc = m["account"]
        if acc in _available_accounts:
            continue
        try:
            r = subprocess.run(
                ["gcloud", "auth", "print-access-token", f"--account={acc}"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0 and r.stdout.strip().startswith("ya29."):
                _available_accounts.add(acc)
        except Exception:
            pass

    log.info(f"Available gcloud accounts: {_available_accounts or 'NONE'}")
    for pid in PEER_IDS:
        acc = MACHINES[pid]["account"]
        if acc in _available_accounts:
            log.info(f"  Can wake {pid} (have {acc})")
        else:
            log.warning(f"  CANNOT wake {pid} (missing {acc})")


def can_wake(pid):
    return MACHINES[pid]["account"] in _available_accounts


_token_cache = {}
TOKEN_CACHE_TTL = 2700  # 45 min


def get_token(account):
    if account in _token_cache:
        token, expires = _token_cache[account]
        if _now_ts() < expires:
            return token
    try:
        r = subprocess.run(
            ["gcloud", "auth", "print-access-token", f"--account={account}"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            token = r.stdout.strip()
            if token:
                _token_cache[account] = (token, _now_ts() + TOKEN_CACHE_TTL)
                return token
    except Exception as e:
        log.error(f"Token fetch failed for {account}: {e}")
    return None


# ===================================================================
# HTTP HELPERS
# ===================================================================
def http_get(url, timeout=15, headers=None):
    hdrs = headers or {}
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def http_post_json(url, data, timeout=15, headers=None):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    payload = json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


# ===================================================================
# HEARTBEAT
# ===================================================================
def send_heartbeat(pid):
    m = MACHINES[pid]
    url = f"https://8080-{m['web_host']}/heartbeat"

    token = get_token(m["account"])
    if not token:
        log.warning(f"[HB] {pid}: no token for {m['account']}, skip")
        return False

    try:
        payload = random_payload()
        payload["ledger"] = load_ledger()

        auth_headers = {"Authorization": f"Bearer {token}"}
        status, resp = http_post_json(url, payload, timeout=20, headers=auth_headers)

        if isinstance(resp, dict) and "ledger" in resp:
            local = load_ledger()
            merged = merge_ledgers(local, resp["ledger"])
            save_ledger(merged)

        state.mark_alive(pid)
        log.info(f"[HB] {pid} alive ({payload['tag']})")
        return True

    except urllib.error.HTTPError as e:
        if e.code in (400, 404, 405, 500):
            state.mark_alive(pid)
            log.info(f"[HB] {pid} alive (HTTP {e.code}, server up but endpoint issue)")
            return True
        state.mark_fail(pid)
        log.warning(f"[HB] {pid} fail: HTTP {e.code}")
        return False

    except Exception as e:
        state.mark_fail(pid)
        log.warning(f"[HB] {pid} fail: {e}")
        return False


# ===================================================================
# WAKE
# ===================================================================
def wake_machine_once(pid):
    m = MACHINES[pid]
    acc = m["account"]

    if not can_wake(pid):
        log.warning(f"[WAKE] Cannot wake {pid}: no token for {acc}")
        return False

    token = get_token(acc)
    if not token:
        log.error(f"[WAKE] Token fetch failed for {acc}")
        return False

    log.info(f"[WAKE] Visiting {pid} ({m['workspace']})...")

    try:
        req = urllib.request.Request(
            f"https://{m['web_host']}/",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            log.info(f"[WAKE] {pid} visit: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        log.info(f"[WAKE] {pid} visit: HTTP {e.code} (expected during startup)")
    except Exception as e:
        log.warning(f"[WAKE] {pid} visit error: {e}")

    try:
        req = urllib.request.Request(
            f"https://8080-{m['web_host']}/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            log.info(f"[WAKE] {pid} port 8080: HTTP {resp.status}")
    except Exception:
        pass

    record_visit(SELF_ID, pid)
    state.record_wake(pid)
    return True


def wake_with_retry(pid):
    ps = state.get(pid)
    last_wake = ps.get("last_wake", 0)
    if _now_ts() - last_wake < WAKE_COOLDOWN_S:
        remaining = int(WAKE_COOLDOWN_S - (_now_ts() - last_wake))
        log.info(f"[WAKE] {pid} on cooldown ({remaining}s left), skipping")
        return False

    for attempt, delay in enumerate(WAKE_RETRY_DELAYS, 1):
        log.info(f"[WAKE] {pid} attempt {attempt}/{len(WAKE_RETRY_DELAYS)}")

        if not wake_machine_once(pid):
            log.error(f"[WAKE] {pid} cannot send wake request")
            return False

        log.info(f"[WAKE] Waiting {delay}s for {pid} to boot...")
        time.sleep(delay)

        if send_heartbeat(pid):
            log.info(f"[WAKE] {pid} is UP after attempt {attempt}!")
            return True

        log.warning(f"[WAKE] {pid} still down after attempt {attempt}")

    log.error(f"[WAKE] {pid} FAILED after {len(WAKE_RETRY_DELAYS)} attempts")
    return False


# ===================================================================
# MAIN LOOPS
# ===================================================================
_wake_in_progress = set()
_wake_lock = threading.Lock()


def _start_wake_thread(pid):
    with _wake_lock:
        if pid in _wake_in_progress:
            return
        _wake_in_progress.add(pid)

    def _do_wake():
        try:
            wake_with_retry(pid)
        finally:
            with _wake_lock:
                _wake_in_progress.discard(pid)

    t = threading.Thread(target=_do_wake, daemon=True, name=f"wake-{pid}")
    t.start()


def heartbeat_loop():
    log.info("[HB-LOOP] Starting heartbeat loop")
    while True:
        peers = list(PEER_IDS)
        random.shuffle(peers)

        for pid in peers:
            alive = send_heartbeat(pid)
            if not alive:
                ps = state.get(pid)
                fails = ps.get("consecutive_fails", 0)
                if fails >= HEARTBEAT_FAIL_THRESHOLD:
                    log.warning(f"[HB-LOOP] {pid} failed {fails} heartbeats, triggering emergency wake")
                    _start_wake_thread(pid)
            time.sleep(random.uniform(2, 8))

        if state.is_survivor_mode():
            interval = random.uniform(SURVIVOR_HEARTBEAT_S * 0.8, SURVIVOR_HEARTBEAT_S * 1.2)
            log.info(f"[HB-LOOP] SURVIVOR MODE ({state.count_dead()} peers down), next in {interval:.0f}s")
        else:
            interval = random.uniform(HEARTBEAT_MIN_S, HEARTBEAT_MAX_S)

        time.sleep(interval)


def visit_loop():
    delay = random.uniform(60, 180)
    log.info(f"[VISIT-LOOP] Starting in {delay:.0f}s")
    time.sleep(delay)

    while True:
        peers = list(PEER_IDS)
        random.shuffle(peers)

        survivor = state.is_survivor_mode()
        effective_interval = SURVIVOR_WAKE_S if survivor else VISIT_INTERVAL_S
        effective_grace = effective_interval * 0.75

        for pid in peers:
            if was_recently_visited(pid, grace_s=effective_grace):
                continue

            ps = state.get(pid)
            if ps.get("alive"):
                log.info(f"[VISIT] Preventive visit to {pid} (alive)")
                wake_machine_once(pid)
            else:
                log.info(f"[VISIT] {pid} appears down, full wake")
                _start_wake_thread(pid)

            time.sleep(random.uniform(30, 90))

        cleanup_ledger()

        if survivor:
            sleep_s = random.uniform(SURVIVOR_WAKE_S * 0.8, SURVIVOR_WAKE_S * 1.2)
        else:
            sleep_s = random.uniform(VISIT_INTERVAL_S * 0.8, VISIT_INTERVAL_S * 1.2)

        log.info(f"[VISIT-LOOP] Next cycle in {sleep_s / 60:.0f}min")
        time.sleep(sleep_s)


def status_summary():
    return {
        "self": SELF_ID,
        "uptime": int(time.time() - START_TIME),
        "config": _config_path,
        "machines": len(MACHINES),
        "accounts": list(_available_accounts),
        "peers": {
            pid: {
                "alive": state.get(pid).get("alive"),
                "last_seen": state.get(pid).get("last_seen", 0),
                "consecutive_fails": state.get(pid).get("consecutive_fails", 0),
                "wake_attempts": state.get(pid).get("wake_attempts", 0),
                "can_wake": can_wake(pid),
            }
            for pid in PEER_IDS
        },
        "survivor_mode": state.is_survivor_mode(),
        "dead_count": state.count_dead(),
        "ledger_entries": len(load_ledger()),
    }


# ===================================================================
# ENTRY POINT
# ===================================================================
def main():
    if not MACHINES:
        log.error("No machines loaded! Place machines.json in /tmp/ or config/ directory.")
        sys.exit(1)

    if not SELF_ID:
        log.error("Cannot detect self! Set ALARM_SELF_ID env var or /tmp/alarm_self_id file.")
        log.error(f"Known machines: {list(MACHINES.keys())}")
        sys.exit(1)

    log.info("=" * 60)
    log.info(f"Alarm Mesh v2.0 starting as [{SELF_ID}]")
    log.info(f"Config: {_config_path}")
    log.info(f"Machines: {list(MACHINES.keys())} ({len(MACHINES)} total)")
    log.info(f"Peers: {PEER_IDS}")
    log.info("=" * 60)

    detect_accounts()

    t_hb = threading.Thread(target=heartbeat_loop, daemon=True, name="heartbeat-loop")
    t_hb.start()

    try:
        visit_loop()
    except KeyboardInterrupt:
        log.info("Shutting down (Ctrl+C)")
    except Exception as e:
        log.error(f"Fatal error in visit loop: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
