#!/usr/bin/env python3
"""
Deploy alarm mesh to Firebase Studio machines.
Gets SSH tokens via HTTP API (no hardcoded tmate tokens!).

Usage:
    python3 deploy.py A          # Deploy to machine A
    python3 deploy.py A B C D    # Deploy to specific machines
    python3 deploy.py ALL        # Deploy to all machines
    python3 deploy.py --config   # Only update machines.json on all machines
"""

import pty, os, time, select, re, base64, gzip, sys, json, subprocess
from pathlib import Path

ANSI = re.compile(r'\x1b[\[\(][0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[>=]')


def clean(b):
    return ANSI.sub('', b.decode('utf-8', 'replace'))


SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent
CONFIG_PATH = REPO_DIR / "config" / "machines.json"

# Load machine config
with open(CONFIG_PATH) as f:
    config = json.load(f)

MACHINES = {m["id"]: m for m in config["machines"]}


def get_gcloud_token(account):
    """Get OAuth2 Bearer token for accessing cloudworkstations.dev proxy."""
    try:
        r = subprocess.run(
            ["gcloud", "auth", "print-access-token", f"--account={account}"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip().startswith("ya29."):
            return r.stdout.strip()
    except Exception as e:
        print(f"  ERROR getting gcloud token for {account}: {e}")
    return None


def get_ssh_token(machine):
    """Get tmate SSH token via HTTP API (port 8080/links)."""
    import urllib.request
    import urllib.error

    account = machine["account"]
    web_host = machine["web_host"]

    token = get_gcloud_token(account)
    if not token:
        print(f"  No gcloud token for {account}")
        return None

    url = f"https://8080-{web_host}/links"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode()
    except Exception as e:
        print(f"  HTTP API failed ({url}): {e}")
        return None

    for line in text.split("\n"):
        if line.startswith("SSH_RW:"):
            # Extract token from: SSH_RW: ssh -tt ... TOKEN@lon1.tmate.io
            parts = line.split()
            for p in parts:
                if "@lon1.tmate.io" in p:
                    return p.split("@")[0]
    print(f"  Could not parse SSH token from API response")
    return None


def deploy_to_machine(mid, config_only=False):
    """Deploy files to a single machine via SSH."""
    if mid not in MACHINES:
        print(f"ERROR: Unknown machine '{mid}'. Known: {list(MACHINES.keys())}")
        return False

    machine = MACHINES[mid]
    print(f"\n{'='*50}")
    print(f"Deploying to Machine {mid} ({machine.get('name', machine['workspace'])})")
    print(f"{'='*50}")

    # Get SSH token via HTTP API
    ssh_token = get_ssh_token(machine)
    if not ssh_token:
        print(f"  SKIP: Cannot get SSH token for {mid}")
        return False

    print(f"  SSH token: {ssh_token[:8]}...")

    # Prepare files to send
    files_to_send = {}

    # Always send machines.json
    files_to_send["machines.json"] = CONFIG_PATH.read_text()

    if not config_only:
        # Send alarm_mesh.py and link_server.py
        alarm_path = REPO_DIR / "scripts" / "alarm_mesh.py"
        ls_path = REPO_DIR / "scripts" / "link_server.py"

        if alarm_path.exists():
            files_to_send["alarm_mesh.py"] = alarm_path.read_text()
        if ls_path.exists():
            files_to_send["link_server.py"] = ls_path.read_text()

    # Compress each file
    compressed = {}
    for name, content in files_to_send.items():
        b64 = base64.b64encode(gzip.compress(content.encode())).decode()
        compressed[name] = b64
        print(f"  {name}: {len(content)} bytes → {len(b64)} chars compressed")

    # Build deploy script
    deploy_lines = [f'#!/bin/bash', f'echo "=== Deploy to {mid} ==="']

    for name, b64_data in compressed.items():
        tmp_name = name.replace(".", "_")
        deploy_lines.append(f'echo -n "" > /tmp/_{tmp_name}.b64')
        # Will be sent in chunks below

    if not config_only:
        deploy_lines.extend([
            f'echo "{mid}" > /tmp/alarm_self_id',
            '',
            '# Stop old processes (NOT tmate!)',
            'pkill -f "alarm_mesh" 2>/dev/null || true',
            'pkill -f "link_server" 2>/dev/null || true',
            'pkill -f "alarm-watchdog" 2>/dev/null || true',
            'sleep 2',
        ])

    # Decompress files
    for name, b64_data in compressed.items():
        tmp_name = name.replace(".", "_")
        deploy_lines.append(f'base64 -d /tmp/_{tmp_name}.b64 | gzip -d > /tmp/{name}')
        deploy_lines.append(f'echo "{name}: $(wc -c < /tmp/{name}) bytes"')

    if not config_only:
        deploy_lines.extend([
            '',
            '# Find python3 (NEVER use command -v python3 on Nix — triggers picker!)',
            'PY=$(ls /nix/store/*/bin/python3 2>/dev/null | grep 3.11 | head -1)',
            '[ -z "$PY" ] && PY=$(ls /nix/store/*/bin/python3 2>/dev/null | head -1)',
            'if [ -n "$PY" ]; then ln -sf "$PY" /tmp/python3; fi',
            'PY="/tmp/python3"',
            '',
            '# Start link_server',
            'nohup $PY /tmp/link_server.py > /tmp/link_server.log 2>&1 & disown',
            'echo "link_server PID=$!"',
            '',
            f'# Start alarm_mesh',
            f'ALARM_SELF_ID={mid} nohup $PY /tmp/alarm_mesh.py >> /tmp/alarm_mesh.log 2>&1 & disown',
            'echo "alarm_mesh PID=$!"',
            '',
            '# Start watchdog',
            f'cat > /tmp/alarm-watchdog.sh << \'WDEOF\'',
            'while true; do',
            '    sleep 60',
            '    PY=/tmp/python3',
            '    if ! pgrep -f "link_server" > /dev/null 2>&1; then',
            '        nohup $PY /tmp/link_server.py > /tmp/link_server.log 2>&1 & disown',
            '    fi',
            '    if ! pgrep -f "alarm_mesh" > /dev/null 2>&1; then',
            f'        ALARM_SELF_ID={mid} nohup $PY /tmp/alarm_mesh.py >> /tmp/alarm_mesh.log 2>&1 & disown',
            '    fi',
            'done',
            'WDEOF',
            'pkill -f "alarm-watchdog" 2>/dev/null || true',
            'nohup bash /tmp/alarm-watchdog.sh > /dev/null 2>&1 & disown',
        ])

    deploy_lines.extend([
        '',
        'sleep 2',
        'echo "--- Processes ---"',
        'pgrep -la python || echo "no python processes"',
        'curl -s http://localhost:8080/health && echo " (health OK)" || echo "health FAIL"',
        'echo "=== DEPLOY_DONE ==="',
    ])

    deploy_script = '\n'.join(deploy_lines)

    # Connect via SSH
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("ssh", ["ssh", "-tt",
            "-o", "SetEnv TERM=xterm-256color",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15",
            f"{ssh_token}@lon1.tmate.io"])
    else:
        def read(timeout=5):
            buf = b""
            end = time.time() + timeout
            while time.time() < end:
                r, _, _ = select.select([fd], [], [], 0.5)
                if r:
                    try:
                        buf += os.read(fd, 8192)
                    except Exception:
                        break
            return buf

        def write(s):
            try:
                os.write(fd, s.encode() if isinstance(s, str) else s)
                return True
            except Exception:
                print("  WRITE FAILED - connection lost")
                return False

        def cmd(c, wait=1):
            if not write(c + "\n"):
                return ""
            time.sleep(wait)
            return clean(read(2))

        # Connect and clear prompt
        read(15)
        time.sleep(3)
        read(2)
        write(b"\x03\x03")
        time.sleep(1)
        read(1)
        print("  Connected!")

        # Send compressed files as base64 chunks
        for name, b64_data in compressed.items():
            tmp_name = name.replace(".", "_")
            print(f"  Sending {name}...")
            cmd(f"rm -f /tmp/_{tmp_name}.b64", 0.3)
            chunks = [b64_data[i:i + 800] for i in range(0, len(b64_data), 800)]
            for i, c in enumerate(chunks):
                write(f"echo -n '{c}' >> /tmp/_{tmp_name}.b64\n")
                time.sleep(0.15)
                if i % 8 == 7:
                    read(0.3)
            time.sleep(0.5)
            read(0.5)
            print(f"    {len(chunks)} chunks sent")

        # Send and execute deploy script
        print("  Sending deploy script...")
        deploy_b64 = base64.b64encode(deploy_script.encode()).decode()
        cmd("rm -f /tmp/_deploy.b64", 0.3)
        chunks = [deploy_b64[i:i + 800] for i in range(0, len(deploy_b64), 800)]
        for i, c in enumerate(chunks):
            write(f"echo -n '{c}' >> /tmp/_deploy.b64\n")
            time.sleep(0.15)
            if i % 8 == 7:
                read(0.3)
        time.sleep(0.5)
        read(0.5)
        cmd("base64 -d /tmp/_deploy.b64 > /tmp/deploy.sh", 0.5)

        print("  Running deploy...")
        cmd("bash /tmp/deploy.sh", 3)

        # Wait for DEPLOY_DONE
        success = False
        for _ in range(20):
            out = read(3)
            text = clean(out)
            for line in text.split('\n'):
                l = line.strip()
                if l and any(w in l for w in ['alarm_mesh', 'link_server', 'DEPLOY', 'bytes', 'PID', 'health', 'NOT']):
                    print(f"    {l}")
            if 'DEPLOY_DONE' in text:
                success = True
                break

        # Disconnect (tmux detach, NOT exit)
        write(b"\x02d")
        time.sleep(1)
        try:
            os.kill(pid, 9)
        except Exception:
            pass
        try:
            os.waitpid(pid, 0)
        except Exception:
            pass

        if success:
            print(f"  Machine {mid}: OK")
        else:
            print(f"  Machine {mid}: deploy script may not have completed")

        return success


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print(f"  {sys.argv[0]} A              # Deploy to machine A")
        print(f"  {sys.argv[0]} A B C D        # Deploy to specific machines")
        print(f"  {sys.argv[0]} ALL            # Deploy to all machines")
        print(f"  {sys.argv[0]} --config       # Only update machines.json on all")
        print(f"\nKnown machines: {list(MACHINES.keys())}")
        sys.exit(1)

    config_only = "--config" in sys.argv
    targets = [a for a in sys.argv[1:] if not a.startswith("-")]

    if "ALL" in targets:
        targets = list(MACHINES.keys())

    if not targets and config_only:
        targets = list(MACHINES.keys())

    results = {}
    for mid in targets:
        ok = deploy_to_machine(mid, config_only=config_only)
        results[mid] = "OK" if ok else "FAILED"

    print(f"\n{'='*50}")
    print("Deploy summary:")
    for mid, status in results.items():
        print(f"  {mid}: {status}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
