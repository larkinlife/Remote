#!/bin/bash
# Start services - runs on EVERY workspace start
set -euo pipefail

LOGFILE="/tmp/start.log"
exec > >(tee -a "$LOGFILE") 2>&1

echo "[$(date -u)] Starting services..."

# === tmate SSH access ===
start_tmate() {
    # Don't restart if already running
    if pgrep -f "tmate -F" > /dev/null 2>&1; then
        echo "[tmate] Already running."
        return
    fi

    echo "[tmate] Starting tmate session..."

    # Start tmate in foreground mode, backgrounded
    nohup tmate -f /dev/null -F > /tmp/tmate.log 2>&1 &
    disown

    # Wait for session to be ready
    for i in $(seq 1 30); do
        if [ -f /tmp/tmate.log ] && grep -q "ssh session:" /tmp/tmate.log 2>/dev/null; then
            break
        fi
        sleep 1
    done

    # Extract and save connection info
    if grep -q "ssh session:" /tmp/tmate.log 2>/dev/null; then
        SSH_RW=$(grep "ssh session:" /tmp/tmate.log | head -1 | sed 's/.*ssh session: //')
        WEB_RW=$(grep "web session:" /tmp/tmate.log | head -1 | sed 's/.*web session: //')
        SSH_RO=$(grep "ssh session read only:" /tmp/tmate.log | head -1 | sed 's/.*ssh session read only: //')
        WEB_RO=$(grep "web session read only:" /tmp/tmate.log | head -1 | sed 's/.*web session read only: //')

        # Save to well-known file
        cat > /tmp/tmate_links.txt << EOF
SSH_RW: ssh -tt -o "SetEnv TERM=xterm-256color" -o "ServerAliveInterval=15" -o "ServerAliveCountMax=3" ${SSH_RW}
WEB_RW: ${WEB_RW}
SSH_RO: ssh -tt -o "SetEnv TERM=xterm-256color" -o "ServerAliveInterval=15" -o "ServerAliveCountMax=3" ${SSH_RO}
WEB_RO: ${WEB_RO}
EOF
        echo "[tmate] Session ready!"
        echo "[tmate] SSH: ${SSH_RW}"
        echo "[tmate] Web: ${WEB_RW}"

        # Also save just the token for easy retrieval
        TOKEN=$(echo "$SSH_RW" | awk '{print $2}' | cut -d'@' -f1)
        echo "$TOKEN" > /tmp/tmate_token.txt
    else
        echo "[tmate] WARNING: Could not get session info after 30s"
        echo "[tmate] Log contents:"
        cat /tmp/tmate.log 2>/dev/null
    fi
}

# === Simple HTTP server to serve tmate links ===
start_link_server() {
    # Don't restart if already running
    if pgrep -f "python3.*link_server" > /dev/null 2>&1; then
        echo "[link-server] Already running."
        return
    fi

    cat > /tmp/link_server.py << 'PYEOF'
#!/usr/bin/env python3
"""Simple HTTP server on port 8080 to serve tmate links."""
import http.server
import os

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path == '/links':
            try:
                with open('/tmp/tmate_links.txt') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(content.encode())
            except FileNotFoundError:
                self.send_response(503)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b'tmate not ready yet\n')
        elif self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'ok\n')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress logs

if __name__ == '__main__':
    server = http.server.HTTPServer(('0.0.0.0', 8080), Handler)
    print(f'[link-server] Listening on port 8080')
    server.serve_forever()
PYEOF

    nohup python3 /tmp/link_server.py > /tmp/link_server.log 2>&1 &
    disown
    echo "[link-server] Started on port 8080"
}

# === Watchdog - restart services if they die ===
start_watchdog() {
    if pgrep -f "alarm-watchdog" > /dev/null 2>&1; then
        echo "[watchdog] Already running."
        return
    fi

    cat > /tmp/alarm-watchdog.sh << 'WDEOF'
#!/bin/bash
# Watchdog: restart tmate and link server if they die
while true; do
    sleep 60
    if ! pgrep -f "tmate -F" > /dev/null 2>&1; then
        echo "[watchdog] tmate died, restarting..." >> /tmp/watchdog.log
        bash /home/user/*/scripts/start.sh 2>/dev/null || true
    fi
    if ! pgrep -f "link_server" > /dev/null 2>&1; then
        echo "[watchdog] link server died, restarting..." >> /tmp/watchdog.log
        nohup python3 /tmp/link_server.py > /tmp/link_server.log 2>&1 &
        disown
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
start_watchdog

echo "[$(date -u)] All services started."
echo "Links available at: https://8080-<WORKSPACE>.cloudworkstations.dev/links"
