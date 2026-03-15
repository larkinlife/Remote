#!/usr/bin/env python3
"""
Deploy alarm mesh via HTTP /exec endpoint (no SSH needed).
Uses gcloud tokens or generateAccessToken WS JWT tokens.

Usage:
    python3 deploy_http.py A          # Deploy to machine A
    python3 deploy_http.py ALL        # Deploy to all machines
    python3 deploy_http.py --status   # Check all machines
"""

import json, sys, os, subprocess, base64, gzip, time
from pathlib import Path
import urllib.request, urllib.error

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent
CONFIG_PATH = REPO_DIR / "config" / "machines.json"

with open(CONFIG_PATH) as f:
    config = json.load(f)

MACHINES = {m["id"]: m for m in config["machines"]}
EXEC_SECRET = "vps123-exec-key"


def get_gcloud_token(account):
    try:
        r = subprocess.run(
            ["gcloud", "auth", "print-access-token", f"--account={account}"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip().startswith("ya29."):
            return r.stdout.strip(), "gcloud"
    except Exception as e:
        print(f"  gcloud token failed for {account}: {e}")
    return None, None


def get_ws_token(machine):
    """Generate a Workstation JWT token via API."""
    account = machine["account"]
    gcloud_token, _ = get_gcloud_token(account)
    if not gcloud_token:
        return None, None

    project = machine.get("project", "monospace-13")
    workspace = machine["workspace"]

    url = (
        f"https://workstations.googleapis.com/v1/"
        f"projects/{project}/locations/europe-west4/"
        f"workstationClusters/workstation-cluster-4/"
        f"workstationConfigs/monospace-config-web/"
        f"workstations/{workspace}:generateAccessToken"
    )

    try:
        data = json.dumps({}).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Authorization": f"Bearer {gcloud_token}",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return result.get("accessToken"), "ws_jwt"
    except Exception as e:
        print(f"  WS token failed: {e}")
    return None, None


def get_best_token(machine):
    """Get the best available token for a machine."""
    # Try gcloud first (faster, simpler)
    token, kind = get_gcloud_token(machine["account"])
    if token:
        return token, kind
    # Fall back to WS JWT
    return get_ws_token(machine)


def http_request(url, data=None, headers=None, timeout=30):
    """Make HTTP request, return (status, body) or (error_code, error_msg)."""
    hdrs = headers or {}
    if data:
        hdrs["Content-Type"] = "application/json"
        payload = json.dumps(data).encode()
        req = urllib.request.Request(url, data=payload, headers=hdrs)
    else:
        req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:500]
        except Exception:
            pass
        return e.code, body
    except Exception as e:
        return 0, str(e)


def exec_remote(mid, script, timeout=60):
    """Execute a script on a remote machine via /exec endpoint."""
    machine = MACHINES[mid]
    web_host = machine["web_host"]

    token, kind = get_best_token(machine)
    if not token:
        return None, "no token"

    url = f"https://8080-{web_host}/exec"
    status, result = http_request(url, {
        "secret": EXEC_SECRET,
        "script": script,
        "timeout": timeout,
    }, headers={"Authorization": f"Bearer {token}"}, timeout=timeout + 15)

    if status == 200 and isinstance(result, dict):
        return result, None
    return None, f"HTTP {status}: {result}"


def check_status(mid):
    """Check machine health."""
    machine = MACHINES[mid]
    web_host = machine["web_host"]

    token, kind = get_best_token(machine)
    if not token:
        return {"status": "NO_TOKEN"}

    # Check /health
    url = f"https://8080-{web_host}/health"
    status, body = http_request(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)

    if status != 200:
        return {"status": "DOWN", "http": status}

    # Get /status
    url = f"https://8080-{web_host}/status"
    status, body = http_request(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if status == 200 and isinstance(body, dict):
        return {"status": "ALIVE", **body}
    return {"status": "ALIVE_NO_STATUS"}


def deploy_to_machine(mid):
    """Deploy files to a machine via /exec endpoint."""
    machine = MACHINES[mid]
    name = machine.get("name", machine["workspace"])
    print(f"\n{'='*50}")
    print(f"Deploying to Machine {mid} ({name})")
    print(f"{'='*50}")

    # Check if /exec is available
    result, err = exec_remote(mid, "echo EXEC_OK", timeout=10)
    if err or not result or result.get("stdout", "").strip() != "EXEC_OK":
        print(f"  /exec not available: {err or result}")
        print(f"  Cannot deploy without /exec endpoint. Deploy via SSH first.")
        return False

    print(f"  /exec endpoint available!")

    # Prepare files
    files = {}
    for fname in ["alarm_mesh.py", "link_server.py", "start.sh", "gcloud_auth_monitor.py"]:
        path = REPO_DIR / "scripts" / fname
        if path.exists():
            files[fname] = path.read_text()

    files["machines.json"] = CONFIG_PATH.read_text()

    # Deploy each file via /exec (base64-encoded)
    for fname, content in files.items():
        b64 = base64.b64encode(content.encode()).decode()
        target = f"/tmp/{fname}"
        script = f"echo '{b64}' | base64 -d > {target} && echo 'OK {fname} '$(wc -c < {target})' bytes'"
        result, err = exec_remote(mid, script, timeout=15)
        if err:
            print(f"  FAILED to deploy {fname}: {err}")
            return False
        stdout = result.get("stdout", "").strip()
        print(f"  {stdout}")

    # Copy to persistent location
    script = """
mkdir -p "$HOME/Remote/scripts" "$HOME/Remote/config"
cp /tmp/alarm_mesh.py "$HOME/Remote/scripts/"
cp /tmp/link_server.py "$HOME/Remote/scripts/"
cp /tmp/start.sh "$HOME/Remote/scripts/" && chmod +x "$HOME/Remote/scripts/start.sh"
cp /tmp/gcloud_auth_monitor.py "$HOME/Remote/scripts/"
cp /tmp/machines.json "$HOME/Remote/config/"
echo "Files copied to ~/Remote"
"""
    result, err = exec_remote(mid, script, timeout=15)
    if err:
        print(f"  Copy failed: {err}")
    else:
        print(f"  {result.get('stdout', '').strip()}")

    # Set self ID and restart services
    script = f"""
echo '{mid}' > /tmp/alarm_self_id
pkill -f 'alarm_mesh' 2>/dev/null || true
pkill -f 'link_server' 2>/dev/null || true
pkill -f 'alarm-watchdog' 2>/dev/null || true
sleep 2
ALARM_SKIP_WATCHDOG=0 bash "$HOME/Remote/scripts/start.sh" 2>&1 | tail -20
echo "=== DEPLOY_DONE ==="
"""
    print("  Restarting services...")
    result, err = exec_remote(mid, script, timeout=60)
    if err:
        print(f"  Restart failed: {err}")
        return False

    stdout = result.get("stdout", "")
    for line in stdout.split("\n"):
        line = line.strip()
        if line:
            print(f"  {line}")

    success = "DEPLOY_DONE" in stdout
    print(f"  {'OK' if success else 'INCOMPLETE'}")
    return success


def cmd_status():
    print("Checking all machines...\n")
    for mid, machine in MACHINES.items():
        name = machine.get("name", machine["workspace"])
        s = check_status(mid)
        status = s.get("status", "UNKNOWN")
        uptime = s.get("server_uptime", "?")
        local = s.get("local_health", {})
        print(f"  {mid} ({name}): {status} (uptime: {uptime}s)")
        if local:
            down_svcs = [k for k, v in local.items() if v != "up"]
            if down_svcs:
                print(f"    DOWN services: {', '.join(down_svcs)}")


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print(f"  {sys.argv[0]} --status         # Check all machines")
        print(f"  {sys.argv[0]} A                 # Deploy to machine A")
        print(f"  {sys.argv[0]} ALL               # Deploy to all machines")
        print(f"\nKnown machines: {list(MACHINES.keys())}")
        sys.exit(1)

    if "--status" in sys.argv:
        cmd_status()
        return

    targets = sys.argv[1:]
    if "ALL" in targets:
        targets = list(MACHINES.keys())

    results = {}
    for mid in targets:
        if mid.startswith("-"):
            continue
        if mid not in MACHINES:
            print(f"Unknown machine: {mid}")
            continue
        ok = deploy_to_machine(mid)
        results[mid] = "OK" if ok else "FAILED"

    print(f"\n{'='*50}")
    print("Deploy summary:")
    for mid, status in results.items():
        print(f"  {mid}: {status}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
