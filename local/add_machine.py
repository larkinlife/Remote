#!/usr/bin/env python3
"""
Add a new Firebase Studio machine to the alarm mesh.

Usage:
    python3 add_machine.py <ssh_token>
    python3 add_machine.py <ssh_token> --account user@gmail.com

What it does:
1. Connects to the new machine via SSH
2. Detects workspace ID, web_host, and gcloud account
3. Assigns next available letter (E, F, G...)
4. Updates machines.json
5. Deploys alarm mesh to the new machine
6. Updates machines.json on ALL existing machines
7. Checks if new account needs to be added to existing machines
"""

import pty, os, time, select, re, json, sys, subprocess, string
from pathlib import Path

ANSI = re.compile(r'\x1b[\[\(][0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[>=]')


def clean(b):
    return ANSI.sub('', b.decode('utf-8', 'replace'))


SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_DIR = SCRIPT_DIR.parent
CONFIG_PATH = REPO_DIR / "config" / "machines.json"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print(f"  Config saved to {CONFIG_PATH}")


def next_machine_id(config):
    """Find next available letter."""
    used = {m["id"] for m in config["machines"]}
    for letter in string.ascii_uppercase:
        if letter not in used:
            return letter
    raise RuntimeError("All 26 letters used!")


def ssh_detect(ssh_token):
    """Connect via SSH and detect machine details."""
    print(f"\nConnecting to {ssh_token[:8]}...@lon1.tmate.io")

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("ssh", ["ssh", "-tt",
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

        def cmd(c, wait=2):
            os.write(fd, (c + "\n").encode())
            time.sleep(wait)
            return clean(read(3))

        # Wait for connection
        read(15)
        time.sleep(3)
        read(2)
        os.write(fd, b"\x03\x03")
        time.sleep(1)
        read(1)
        print("  Connected!")

        info = {}

        # Get WEB_HOST
        out = cmd("echo WH=$WEB_HOST", 2)
        for line in out.split("\n"):
            if line.strip().startswith("WH=") and "." in line:
                info["web_host"] = line.strip().split("=", 1)[1].strip()

        # Get hostname / workspace name
        out = cmd("cat /proc/sys/kernel/hostname 2>/dev/null || echo UNKNOWN", 2)
        for line in out.split("\n"):
            line = line.strip()
            if line and "firebase" in line.lower():
                info["workspace"] = line
                break

        # If WEB_HOST not set, try to derive from workspace
        if "web_host" not in info and "workspace" in info:
            # Try to find it from env or config
            out = cmd("env | grep -i cluster 2>/dev/null; env | grep -i web_host 2>/dev/null; cat /etc/environment 2>/dev/null | grep -i host", 3)
            for line in out.split("\n"):
                if "cloudworkstations.dev" in line:
                    # Extract the host part
                    match = re.search(r'(firebase-\S+\.cloudworkstations\.dev)', line)
                    if match:
                        info["web_host"] = match.group(1)

        # Get active gcloud accounts
        out = cmd("gcloud auth list --format='value(account)' --filter='status:ACTIVE' 2>/dev/null", 5)
        accounts = []
        for line in out.split("\n"):
            line = line.strip()
            if "@" in line and "." in line and "gcloud" not in line and "auth" not in line:
                accounts.append(line)
        info["accounts"] = accounts

        # Get project
        out = cmd("gcloud config get-value project 2>/dev/null", 3)
        for line in out.split("\n"):
            line = line.strip()
            if line.startswith("monospace") or line.startswith("project"):
                info["project"] = line
                break

        # Disconnect (tmux detach)
        os.write(fd, b"\x02d")
        time.sleep(1)
        try:
            os.kill(pid, 9)
        except Exception:
            pass
        try:
            os.waitpid(pid, 0)
        except Exception:
            pass

        return info


def add_gcloud_account_on_machine(ssh_token, account):
    """Run gcloud auth login for an account on a remote machine.
    Returns auth URL for user to visit."""
    print(f"\n  Adding gcloud account {account} on remote machine...")
    print(f"  NOTE: This requires manual browser auth.")
    print(f"  Run on the machine: gcloud auth login {account} --no-launch-browser")
    print(f"  Then paste the auth code.")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print(f"  {sys.argv[0]} <ssh_token>")
        print(f"  {sys.argv[0]} <ssh_token> --account user@gmail.com")
        print(f"\nExample:")
        print(f"  {sys.argv[0]} AbCdEfGhIjKlMnOpQrStU")
        sys.exit(1)

    ssh_token = sys.argv[1]
    forced_account = None
    if "--account" in sys.argv:
        idx = sys.argv.index("--account")
        if idx + 1 < len(sys.argv):
            forced_account = sys.argv[idx + 1]

    # Step 1: Detect machine info
    print("=" * 50)
    print("Step 1: Detecting machine info...")
    print("=" * 50)

    info = ssh_detect(ssh_token)
    print(f"\n  Detected:")
    print(f"    workspace: {info.get('workspace', '?')}")
    print(f"    web_host:  {info.get('web_host', '?')}")
    print(f"    accounts:  {info.get('accounts', [])}")
    print(f"    project:   {info.get('project', '?')}")

    if not info.get("web_host"):
        print("\n  ERROR: Could not detect web_host.")
        print("  Please provide it manually or check the Firebase Studio URL.")
        web_host = input("  Enter web_host (or empty to abort): ").strip()
        if not web_host:
            sys.exit(1)
        info["web_host"] = web_host

    # Determine account
    account = forced_account
    if not account and info.get("accounts"):
        account = info["accounts"][0]
    if not account:
        account = input("  Enter Google account email: ").strip()
        if not account:
            sys.exit(1)

    # Step 2: Assign ID and update config
    print("\n" + "=" * 50)
    print("Step 2: Updating machines.json...")
    print("=" * 50)

    config = load_config()
    mid = next_machine_id(config)

    # Derive name from workspace
    workspace = info.get("workspace", "")
    name = workspace
    for prefix in ["firebase-"]:
        if name.startswith(prefix):
            name = name[len(prefix):]

    new_machine = {
        "id": mid,
        "workspace": workspace,
        "web_host": info["web_host"],
        "account": account,
        "project": info.get("project", ""),
        "name": name,
    }

    print(f"\n  New machine:")
    print(f"    ID:        {mid}")
    print(f"    workspace: {new_machine['workspace']}")
    print(f"    web_host:  {new_machine['web_host']}")
    print(f"    account:   {new_machine['account']}")
    print(f"    project:   {new_machine['project']}")

    config["machines"].append(new_machine)

    # Add account if new
    if account not in config.get("accounts", []):
        config.setdefault("accounts", []).append(account)
        print(f"\n  NEW account added: {account}")

    save_config(config)

    # Step 3: Deploy to new machine
    print("\n" + "=" * 50)
    print(f"Step 3: Deploying alarm mesh to {mid}...")
    print("=" * 50)

    # Import and run deploy
    sys.path.insert(0, str(SCRIPT_DIR))
    from deploy import deploy_to_machine
    deploy_to_machine(mid)

    # Step 4: Update config on all existing machines
    print("\n" + "=" * 50)
    print("Step 4: Updating machines.json on all existing machines...")
    print("=" * 50)

    existing = [m["id"] for m in config["machines"] if m["id"] != mid]
    for eid in existing:
        print(f"\n  Updating {eid}...")
        deploy_to_machine(eid, config_only=True)

    # Step 5: Check gcloud auth
    print("\n" + "=" * 50)
    print("Step 5: Checking gcloud auth...")
    print("=" * 50)

    all_accounts = set(config.get("accounts", []))
    print(f"\n  All accounts in mesh: {all_accounts}")
    print(f"\n  Each machine needs ALL accounts for cross-waking.")
    print(f"  If a new account was added, run on each machine:")
    for acc in all_accounts:
        print(f"    gcloud auth login {acc} --no-launch-browser")

    # Summary
    print("\n" + "=" * 50)
    print("DONE!")
    print("=" * 50)
    print(f"\n  Machine {mid} added to mesh.")
    print(f"  Total machines: {len(config['machines'])}")
    print(f"  machines.json updated on all live machines.")
    print(f"\n  To connect: ./connect.sh {mid}")
    print(f"  To check:   ./connect.sh status")


if __name__ == "__main__":
    main()
