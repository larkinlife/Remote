#!/bin/bash
# Start services — runs on EVERY workspace start (onStart hook)
set -euo pipefail

LOGFILE="/tmp/start.log"
exec > >(tee -a "$LOGFILE") 2>&1

echo "[$(date -u)] Starting services..."

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
START_SHIM="/tmp/start.sh"

find_python() {
    if [ -x /tmp/python3 ] && /tmp/python3 -c "print('ok')" &>/dev/null; then
        echo "/tmp/python3"
        return
    fi

    local nix_py
    nix_py=$(ls /nix/store/*/bin/python3 2>/dev/null | grep '3.11' | head -1)
    if [ -z "$nix_py" ]; then
        nix_py=$(ls /nix/store/*/bin/python3 2>/dev/null | head -1)
    fi
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

ensure_runtime_files() {
    cp "$SCRIPT_DIR/alarm_mesh.py" /tmp/alarm_mesh.py 2>/dev/null || true
    cp "$SCRIPT_DIR/link_server.py" /tmp/link_server.py 2>/dev/null || true
    cp "$SCRIPT_DIR/gcloud_auth_monitor.py" /tmp/gcloud_auth_monitor.py 2>/dev/null || true
    cp "$REPO_DIR/config/machines.json" /tmp/machines.json 2>/dev/null || true

    cat > "$START_SHIM" <<'EOF'
#!/bin/bash
exec bash "$HOME/Remote/scripts/start.sh" "$@"
EOF
    chmod +x "$START_SHIM"
}

publish_tmate_links() {
    local ssh_rw web_rw ssh_ro web_ro token

    if [ ! -f /tmp/tmate.log ]; then
        return 1
    fi

    ssh_rw=$(grep "ssh session:" /tmp/tmate.log | head -1 | sed 's/.*ssh session: //')
    web_rw=$(grep "web session:" /tmp/tmate.log | head -1 | sed 's/.*web session: //')
    ssh_ro=$(grep "ssh session read only:" /tmp/tmate.log | head -1 | sed 's/.*ssh session read only: //')
    web_ro=$(grep "web session read only:" /tmp/tmate.log | head -1 | sed 's/.*web session read only: //')

    if [ -z "$ssh_rw" ] || [ -z "$web_rw" ]; then
        return 1
    fi

    cat > /tmp/tmate_links.txt <<EOF
SSH_RW: ssh -tt -o "SetEnv TERM=xterm-256color" -o "ServerAliveInterval=15" -o "ServerAliveCountMax=3" ${ssh_rw}
WEB_RW: ${web_rw}
SSH_RO: ssh -tt -o "SetEnv TERM=xterm-256color" -o "ServerAliveInterval=15" -o "ServerAliveCountMax=3" ${ssh_ro}
WEB_RO: ${web_ro}
EOF
    token=$(printf '%s\n' "$ssh_rw" | awk '{print $2}' | cut -d'@' -f1)
    if [ -n "$token" ]; then
        printf '%s\n' "$token" > /tmp/tmate_token.txt
    fi
    return 0
}

ensure_runtime_files

find_tmate() {
    # Check if tmate is directly available
    if command -v tmate &>/dev/null; then
        echo "tmate"
        return
    fi
    # Search nix store
    local nix_tmate
    nix_tmate=$(ls /nix/store/*/bin/tmate 2>/dev/null | grep -v '\.drv' | tail -1)
    if [ -n "$nix_tmate" ] && [ -x "$nix_tmate" ]; then
        echo "$nix_tmate"
        return
    fi
    # Check common paths
    for p in /usr/bin/tmate /usr/local/bin/tmate /home/user/.nix-profile/bin/tmate; do
        if [ -x "$p" ]; then
            echo "$p"
            return
        fi
    done
    echo ""
}

start_tmate() {
    if pgrep -f "tmate.*-F" > /dev/null 2>&1; then
        echo "[tmate] Process found, checking health..."
        # If links are already published and fresh, tmate is working
        if [ -s /tmp/tmate_links.txt ] && [ -s /tmp/tmate_token.txt ]; then
            # Check if tmate log has been written to in the last 5 minutes
            if find /tmp/tmate.log -mmin -5 -print 2>/dev/null | grep -q .; then
                echo "[tmate] Already running and healthy."
                return
            fi
        fi
        # tmate process exists but links are missing/stale — kill and restart
        echo "[tmate] Process exists but not healthy (stale after sleep?). Killing..."
        pkill -f "tmate.*-F" 2>/dev/null || true
        sleep 2
        rm -f /tmp/tmate_links.txt /tmp/tmate_token.txt /tmp/tmate.log
    fi

    local TMATE_BIN
    TMATE_BIN=$(find_tmate)
    if [ -z "$TMATE_BIN" ]; then
        echo "[tmate] ERROR: tmate binary not found! Searching nix store..."
        echo "[tmate] Available in /nix/store:"
        ls /nix/store/*/bin/tmate 2>/dev/null || echo "  NONE"
        echo "[tmate] PATH: $PATH"
        echo "[tmate] FATAL: Cannot find tmate. Services will start without SSH access."
        return 1
    fi

    echo "[tmate] Starting tmate session (binary: $TMATE_BIN)..."
    nohup "$TMATE_BIN" -f /dev/null -F > /tmp/tmate.log 2>&1 &
    disown

    for _ in $(seq 1 30); do
        if publish_tmate_links; then
            break
        fi
        sleep 1
    done

    if publish_tmate_links; then
        echo "[tmate] Session ready!"
        grep '^SSH_RW:' /tmp/tmate_links.txt 2>/dev/null | head -1 || true
        grep '^WEB_RW:' /tmp/tmate_links.txt 2>/dev/null | head -1 || true
    else
        echo "[tmate] WARNING: Could not get session info after 30s"
        cat /tmp/tmate.log 2>/dev/null || true
    fi
}

start_link_server() {
    if pgrep -f "link_server.py" > /dev/null 2>&1; then
        echo "[link-server] Already running."
        return
    fi

    if [ ! -f /tmp/link_server.py ]; then
        echo "[link-server] Script not found, skipping."
        return
    fi

    nohup "$PYTHON" /tmp/link_server.py > /tmp/link_server.log 2>&1 &
    disown
    echo "[link-server] Started on port 8080 (PID $!)"
}

start_alarm_mesh() {
    if pgrep -f "alarm_mesh.py" > /dev/null 2>&1; then
        echo "[alarm-mesh] Already running."
        return
    fi

    if [ ! -f /tmp/alarm_mesh.py ]; then
        echo "[alarm-mesh] Script not found, skipping."
        return
    fi

    export ALARM_SELF_ID="${ALARM_SELF_ID:-}"
    if [ -z "$ALARM_SELF_ID" ] && [ -f /tmp/alarm_self_id ]; then
        ALARM_SELF_ID="$(cat /tmp/alarm_self_id 2>/dev/null || true)"
        export ALARM_SELF_ID
    fi

    nohup "$PYTHON" /tmp/alarm_mesh.py >> /tmp/alarm_mesh.log 2>&1 &
    disown
    echo "[alarm-mesh] Started (PID $!)"
}

start_watchdog() {
    if [ "${ALARM_SKIP_WATCHDOG:-0}" = "1" ]; then
        echo "[watchdog] Skipped by ALARM_SKIP_WATCHDOG=1."
        return
    fi

    if pgrep -f "alarm-watchdog" > /dev/null 2>&1; then
        echo "[watchdog] Already running."
        return
    fi

    cat > /tmp/alarm-watchdog.sh <<'WDEOF'
#!/bin/bash
START_SCRIPT="$HOME/Remote/scripts/start.sh"
EVENTS_LOG="/tmp/alarm_events.jsonl"

log_event() {
    local event="$1" detail="$2"
    local ts=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
    local self_id=""
    [ -f /tmp/alarm_self_id ] && self_id=$(cat /tmp/alarm_self_id 2>/dev/null)
    echo "{\"ts\":\"$ts\",\"epoch\":$(date +%s),\"self\":\"$self_id\",\"event\":\"$event\",\"target\":\"$self_id\",\"detail\":\"$detail\"}" >> "$EVENTS_LOG"
}

recover_all() {
    ALARM_SKIP_WATCHDOG=1 bash "$START_SCRIPT" >> /tmp/watchdog.log 2>&1 || true
}

while true; do
    sleep 60
    if [ ! -x "$START_SCRIPT" ]; then
        echo "[$(date -u)] start script missing, waiting..." >> /tmp/watchdog.log
        continue
    fi
    if ! pgrep -f "tmate.*-F" > /dev/null 2>&1; then
        echo "[$(date -u)] tmate died, restarting..." >> /tmp/watchdog.log
        log_event "watchdog_restart" "tmate died, restarting all services"
        recover_all
        continue
    fi
    if [ ! -s /tmp/tmate_links.txt ] || [ ! -s /tmp/tmate_token.txt ]; then
        echo "[$(date -u)] tmate links missing, republishing..." >> /tmp/watchdog.log
        log_event "watchdog_restart" "tmate links missing, republishing"
        recover_all
        continue
    fi
    if [ ! -x /tmp/python3 ] || [ ! -f /tmp/link_server.py ] || [ ! -f /tmp/alarm_mesh.py ] || [ ! -f /tmp/machines.json ]; then
        echo "[$(date -u)] runtime payload missing, rebuilding..." >> /tmp/watchdog.log
        log_event "watchdog_restart" "runtime payload missing, rebuilding"
        recover_all
        continue
    fi
    if ! pgrep -f "link_server.py" > /dev/null 2>&1; then
        echo "[$(date -u)] link server died, restarting..." >> /tmp/watchdog.log
        log_event "watchdog_restart" "link_server died"
        recover_all
        continue
    fi
    if ! pgrep -f "alarm_mesh.py" > /dev/null 2>&1; then
        echo "[$(date -u)] alarm mesh died, restarting..." >> /tmp/watchdog.log
        log_event "watchdog_restart" "alarm_mesh died"
        recover_all
        continue
    fi

    # Rotate watchdog log if too large (>500KB)
    if [ -f /tmp/watchdog.log ] && [ "$(wc -c < /tmp/watchdog.log 2>/dev/null)" -gt 500000 ]; then
        tail -200 /tmp/watchdog.log > /tmp/watchdog.log.tmp
        mv /tmp/watchdog.log.tmp /tmp/watchdog.log
    fi
done
WDEOF

    chmod +x /tmp/alarm-watchdog.sh
    nohup bash /tmp/alarm-watchdog.sh > /dev/null 2>&1 &
    disown
    echo "[watchdog] Started."
}

start_tmate
start_link_server
start_alarm_mesh
start_watchdog

echo "[$(date -u)] All services started."
