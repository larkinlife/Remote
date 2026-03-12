#!/bin/bash
# Start services — runs on EVERY workspace start (onStart hook)
set -euo pipefail

LOGFILE="/tmp/start.log"
exec > >(tee -a "$LOGFILE") 2>&1

echo "[$(date -u)] Starting services..."

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# === Find python3 (Nix puts it in /nix/store/) ===
find_python() {
    if command -v python3 &>/dev/null; then
        echo "python3"
        return
    fi
    local nix_py
    nix_py=$(ls /nix/store/*/bin/python3 2>/dev/null | grep '3.11' | head -1)
    if [ -n "$nix_py" ]; then
        ln -sf "$nix_py" /tmp/python3
        echo "/tmp/python3"
        return
    fi
    nix_py=$(ls /nix/store/*/bin/python3 2>/dev/null | head -1)
    if [ -n "$nix_py" ]; then
        ln -sf "$nix_py" /tmp/python3
        echo "/tmp/python3"
        return
    fi
    echo ""
}

PYTHON=$(find_python)
if [ -z "$PYTHON" ]; then
    echo "[FATAL] No python3 found!"
    exit 1
fi
echo "[python] Using: $PYTHON"

# === Copy scripts to /tmp for stability ===
cp "$SCRIPT_DIR/alarm_mesh.py" /tmp/alarm_mesh.py 2>/dev/null || true
cp "$SCRIPT_DIR/link_server.py" /tmp/link_server.py 2>/dev/null || true
cp "$REPO_DIR/config/machines.json" /tmp/machines.json 2>/dev/null || true

# === tmate SSH access ===
start_tmate() {
    if pgrep -f "tmate -F" > /dev/null 2>&1; then
        echo "[tmate] Already running."
        return
    fi

    echo "[tmate] Starting tmate session..."
    nohup tmate -f /dev/null -F > /tmp/tmate.log 2>&1 &
    disown

    for i in $(seq 1 30); do
        if [ -f /tmp/tmate.log ] && grep -q "ssh session:" /tmp/tmate.log 2>/dev/null; then
            break
        fi
        sleep 1
    done

    if grep -q "ssh session:" /tmp/tmate.log 2>/dev/null; then
        SSH_RW=$(grep "ssh session:" /tmp/tmate.log | head -1 | sed 's/.*ssh session: //')
        WEB_RW=$(grep "web session:" /tmp/tmate.log | head -1 | sed 's/.*web session: //')
        SSH_RO=$(grep "ssh session read only:" /tmp/tmate.log | head -1 | sed 's/.*ssh session read only: //')
        WEB_RO=$(grep "web session read only:" /tmp/tmate.log | head -1 | sed 's/.*web session read only: //')

        cat > /tmp/tmate_links.txt << EOF
SSH_RW: ssh -tt -o "SetEnv TERM=xterm-256color" -o "ServerAliveInterval=15" -o "ServerAliveCountMax=3" ${SSH_RW}
WEB_RW: ${WEB_RW}
SSH_RO: ssh -tt -o "SetEnv TERM=xterm-256color" -o "ServerAliveInterval=15" -o "ServerAliveCountMax=3" ${SSH_RO}
WEB_RO: ${WEB_RO}
EOF
        TOKEN=$(echo "$SSH_RW" | awk '{print $2}' | cut -d'@' -f1)
        echo "$TOKEN" > /tmp/tmate_token.txt
        echo "[tmate] Session ready! SSH: ${SSH_RW}"
    else
        echo "[tmate] WARNING: Could not get session info after 30s"
    fi
}

# === Link server (HTTP API on port 8080) ===
start_link_server() {
    if pgrep -f "link_server" > /dev/null 2>&1; then
        echo "[link-server] Already running."
        return
    fi

    if [ ! -f /tmp/link_server.py ]; then
        echo "[link-server] Script not found, skipping."
        return
    fi

    nohup $PYTHON /tmp/link_server.py > /tmp/link_server.log 2>&1 &
    disown
    echo "[link-server] Started on port 8080 (PID $!)"
}

# === Alarm mesh ===
start_alarm_mesh() {
    if pgrep -f "alarm_mesh.py" > /dev/null 2>&1; then
        echo "[alarm-mesh] Already running."
        return
    fi

    if [ ! -f /tmp/alarm_mesh.py ]; then
        echo "[alarm-mesh] Script not found, skipping."
        return
    fi

    # Auto-detect SELF_ID from /tmp/alarm_self_id if exists
    export ALARM_SELF_ID="${ALARM_SELF_ID:-}"

    nohup $PYTHON /tmp/alarm_mesh.py >> /tmp/alarm_mesh.log 2>&1 &
    disown
    echo "[alarm-mesh] Started (PID $!)"
}

# === Watchdog ===
start_watchdog() {
    if pgrep -f "alarm-watchdog" > /dev/null 2>&1; then
        echo "[watchdog] Already running."
        return
    fi

    cat > /tmp/alarm-watchdog.sh << WDEOF
#!/bin/bash
while true; do
    sleep 60
    if ! pgrep -f "tmate -F" > /dev/null 2>&1; then
        echo "[\$(date -u)] tmate died, restarting..." >> /tmp/watchdog.log
        bash ${SCRIPT_DIR}/start.sh 2>/dev/null || true
    fi
    if ! pgrep -f "link_server" > /dev/null 2>&1; then
        echo "[\$(date -u)] link server died, restarting..." >> /tmp/watchdog.log
        nohup $PYTHON /tmp/link_server.py > /tmp/link_server.log 2>&1 & disown
    fi
    if ! pgrep -f "alarm_mesh" > /dev/null 2>&1; then
        echo "[\$(date -u)] alarm mesh died, restarting..." >> /tmp/watchdog.log
        nohup $PYTHON /tmp/alarm_mesh.py >> /tmp/alarm_mesh.log 2>&1 & disown
    fi
done
WDEOF

    nohup bash /tmp/alarm-watchdog.sh > /dev/null 2>&1 &
    disown
    echo "[watchdog] Started."
}

# Run everything
start_tmate
start_link_server
start_alarm_mesh
start_watchdog

echo "[$(date -u)] All services started."
