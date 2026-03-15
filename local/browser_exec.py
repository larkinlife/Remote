#!/usr/bin/env python3
"""
Execute commands on Firebase Studio machines via browser terminal.
Uses Playwright + Chrome with saved Google auth session.

Usage:
    python3 browser_exec.py B "bash ~/Remote/scripts/start.sh"
    python3 browser_exec.py D "pgrep -la tmate; cat /tmp/tmate.log"
"""

import subprocess, sys, json, time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR.parent / "config" / "machines.json"

with open(CONFIG_PATH) as f:
    config = json.load(f)

MACHINES = {m["id"]: m for m in config["machines"]}


def get_gcloud_token(account):
    try:
        r = subprocess.run(
            ["gcloud", "auth", "print-access-token", f"--account={account}"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip().startswith("ya29."):
            return r.stdout.strip()
    except Exception:
        pass
    return None


def get_ws_token(machine):
    account = machine["account"]
    gcloud_token = get_gcloud_token(account)
    if not gcloud_token:
        return None

    project = machine.get("project", "monospace-13")
    workspace = machine["workspace"]

    import urllib.request
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
            return result.get("accessToken")
    except Exception as e:
        print(f"WS token failed: {e}")
    return None


def run_via_playwright(mid, command):
    """Open VS Code in Playwright and run command in terminal."""
    machine = MACHINES[mid]
    web_host = machine["web_host"]

    # Get WS token for auth
    ws_token = get_ws_token(machine)
    if not ws_token:
        print(f"Cannot get token for {mid}")
        return False

    # Playwright script
    js_script = f"""
const {{ chromium }} = require('playwright');

(async () => {{
    const browser = await chromium.launch({{
        headless: false,
        channel: 'chrome',
    }});
    const context = await browser.newContext();

    // Set the auth cookie/header
    const page = await context.newPage();

    // Set auth header via route interception
    await page.route('**/*', async (route) => {{
        const headers = {{
            ...route.request().headers(),
            'authorization': 'Bearer {ws_token}',
        }};
        await route.continue({{ headers }});
    }});

    console.log('Opening VS Code...');
    await page.goto('https://{web_host}/', {{ waitUntil: 'networkidle', timeout: 60000 }});
    console.log('Page loaded');

    // Wait for VS Code to load
    await page.waitForTimeout(5000);

    // Try to open terminal
    // Ctrl+` opens terminal in VS Code
    await page.keyboard.press('Control+Backquote');
    await page.waitForTimeout(2000);

    // Type command
    const cmd = {json.dumps(command)};
    await page.keyboard.type(cmd, {{ delay: 30 }});
    await page.keyboard.press('Enter');
    console.log('Command sent: ' + cmd);

    // Wait for output
    await page.waitForTimeout(10000);

    // Try to capture terminal output
    const terminalContent = await page.evaluate(() => {{
        const terminals = document.querySelectorAll('.xterm-rows');
        if (terminals.length > 0) {{
            return terminals[terminals.length - 1].textContent;
        }}
        return 'NO TERMINAL FOUND';
    }});
    console.log('Terminal output:');
    console.log(terminalContent);

    await browser.close();
}})();
"""

    # Write and run the script
    script_path = Path("/tmp/browser_exec_script.js")
    script_path.write_text(js_script)

    print(f"Running Playwright for {mid}...")
    r = subprocess.run(
        ["node", str(script_path)],
        capture_output=True, text=True, timeout=120,
        env={**__import__('os').environ, "PLAYWRIGHT_BROWSERS_PATH": "0"},
    )
    print(r.stdout)
    if r.stderr:
        print(f"STDERR: {r.stderr[:500]}")
    return r.returncode == 0


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <machine_id> <command>")
        print(f"Known machines: {list(MACHINES.keys())}")
        sys.exit(1)

    mid = sys.argv[1]
    command = sys.argv[2]

    if mid not in MACHINES:
        print(f"Unknown machine: {mid}")
        sys.exit(1)

    run_via_playwright(mid, command)


if __name__ == "__main__":
    main()
