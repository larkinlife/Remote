#!/bin/bash
# Auto-connect to Firebase Studio machines via permanent HTTP API.
# Reads machine data from machines.json — no hardcoded values.
#
# Usage:
#   ./connect.sh status     # Check all machines
#   ./connect.sh A          # SSH to Machine A
#   ./connect.sh list       # List all machines
#   ./connect.sh ssh A      # Same as ./connect.sh A

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/../config/machines.json"

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: machines.json not found at $CONFIG"
    echo "Expected: <repo>/config/machines.json"
    exit 1
fi

# Parse machines.json with jq (available on Mac via brew, on Nix via dev.nix)
if ! command -v jq &>/dev/null; then
    echo "ERROR: jq not found. Install: brew install jq"
    exit 1
fi

get_machine_ids() {
    jq -r '.machines[].id' "$CONFIG"
}

get_field() {
    local mid="$1" field="$2"
    jq -r ".machines[] | select(.id==\"$mid\") | .$field" "$CONFIG"
}

get_token() {
    gcloud auth print-access-token --account="$1" 2>/dev/null
}

cmd_list() {
    echo "Machines from $CONFIG:"
    echo ""
    jq -r '.machines[] | "  \(.id)  \(.name // .workspace)  (\(.account))"' "$CONFIG"
}

cmd_connect() {
    local mid="$1"
    local web_host=$(get_field "$mid" "web_host")
    local account=$(get_field "$mid" "account")
    local name=$(get_field "$mid" "name // .workspace")

    if [ "$web_host" = "null" ] || [ -z "$web_host" ]; then
        echo "ERROR: Unknown machine '$mid'. Use: ./connect.sh list"
        exit 1
    fi

    echo "Connecting to Machine $mid ($name)..."

    local token=$(get_token "$account")
    if [ -z "$token" ]; then
        echo "ERROR: No gcloud token for $account. Run: gcloud auth login $account"
        return 1
    fi

    local links=$(curl -sf -m 15 -H "Authorization: Bearer $token" "https://8080-${web_host}/links" 2>/dev/null)
    if [ -z "$links" ]; then
        echo "Machine not responding. Sending wake request..."
        curl -sf -m 30 -H "Authorization: Bearer $token" "https://${web_host}/" > /dev/null 2>&1 || true
        echo "Wait 2-3 minutes and try again."
        return 1
    fi

    local ssh_cmd=$(echo "$links" | grep "SSH_RW:" | head -1 | sed 's/SSH_RW: *//')
    if [ -z "$ssh_cmd" ]; then
        echo "Could not parse SSH token from: $links"
        return 1
    fi

    echo "Got fresh SSH link!"
    echo "$ssh_cmd"
    echo ""
    exec $ssh_cmd
}

cmd_status() {
    echo "Checking all machines..."
    echo ""
    for mid in $(get_machine_ids); do
        local web_host=$(get_field "$mid" "web_host")
        local account=$(get_field "$mid" "account")
        local name=$(get_field "$mid" "name // .workspace")

        printf "  %s %-20s " "$mid" "($name):"

        local token=$(get_token "$account" 2>/dev/null)
        if [ -z "$token" ]; then
            echo "NO TOKEN (gcloud auth login $account)"
            continue
        fi

        local health=$(curl -sf -m 10 -H "Authorization: Bearer $token" "https://8080-${web_host}/health" 2>/dev/null || true)
        if [ "$health" = "ok" ]; then
            # Try to get extended status
            local status_json=$(curl -sf -m 10 -H "Authorization: Bearer $token" "https://8080-${web_host}/status" 2>/dev/null || true)
            if [ -n "$status_json" ]; then
                local uptime=$(echo "$status_json" | jq -r '.server_uptime // "?"' 2>/dev/null)
                echo "ALIVE (uptime: ${uptime}s)"
            else
                echo "ALIVE"
            fi
        else
            echo "DOWN / SLEEPING"
        fi
    done
}

cmd_logs() {
    local mid="$1"
    local logfile="${2:-alarm_mesh}"
    local lines="${3:-100}"
    local web_host=$(get_field "$mid" "web_host")
    local account=$(get_field "$mid" "account")
    local name=$(get_field "$mid" "name // .workspace")

    if [ "$web_host" = "null" ] || [ -z "$web_host" ]; then
        echo "ERROR: Unknown machine '$mid'. Use: $0 list"
        exit 1
    fi

    local token=$(get_token "$account")
    if [ -z "$token" ]; then
        echo "ERROR: No gcloud token for $account"
        return 1
    fi

    echo "=== $mid ($name) — $logfile log (last $lines lines) ==="
    curl -sf -m 15 -H "Authorization: Bearer $token" \
        "https://8080-${web_host}/logs?file=${logfile}&lines=${lines}" 2>/dev/null || echo "Failed to get logs (endpoint not deployed or machine down)"
    echo ""
}

cmd_events() {
    local mid="$1"
    local lines="${2:-50}"
    local web_host=$(get_field "$mid" "web_host")
    local account=$(get_field "$mid" "account")
    local name=$(get_field "$mid" "name // .workspace")

    if [ "$web_host" = "null" ] || [ -z "$web_host" ]; then
        echo "ERROR: Unknown machine '$mid'. Use: $0 list"
        exit 1
    fi

    local token=$(get_token "$account")
    if [ -z "$token" ]; then
        echo "ERROR: No gcloud token for $account"
        return 1
    fi

    echo "=== $mid ($name) — events (last $lines) ==="
    curl -sf -m 15 -H "Authorization: Bearer $token" \
        "https://8080-${web_host}/events?lines=${lines}" 2>/dev/null | jq '.events[] | "\(.ts) [\(.event)] \(.detail)"' -r 2>/dev/null || echo "Failed to get events"
    echo ""
}

cmd_logs_all() {
    local logfile="${1:-alarm_mesh}"
    local lines="${2:-30}"
    for mid in $(get_machine_ids); do
        cmd_logs "$mid" "$logfile" "$lines"
    done
}

cmd_events_all() {
    local lines="${1:-30}"
    for mid in $(get_machine_ids); do
        cmd_events "$mid" "$lines"
    done
}

case "${1:-help}" in
    status)  cmd_status ;;
    list)    cmd_list ;;
    ssh)     cmd_connect "${2:?Usage: $0 ssh <ID>}" ;;
    logs)
        if [ "${2:-all}" = "all" ]; then
            cmd_logs_all "${3:-alarm_mesh}" "${4:-30}"
        else
            cmd_logs "${2}" "${3:-alarm_mesh}" "${4:-100}"
        fi
        ;;
    events)
        if [ "${2:-all}" = "all" ]; then
            cmd_events_all "${3:-30}"
        else
            cmd_events "${2}" "${3:-50}"
        fi
        ;;
    help|--help|-h)
        echo "Usage:"
        echo "  $0 <ID>          - Connect to machine via SSH"
        echo "  $0 status        - Check all machines"
        echo "  $0 list          - List all configured machines"
        echo "  $0 logs <ID|all> [file] [lines]  - View logs (alarm_mesh|watchdog|start|tmate|keepalive)"
        echo "  $0 events <ID|all> [lines]       - View structured events"
        ;;
    *)
        # Try as machine ID
        if get_field "$1" "id" | grep -q "$1" 2>/dev/null; then
            cmd_connect "$1"
        else
            echo "Unknown command or machine: $1"
            echo "Use: $0 help  for all commands"
            exit 1
        fi
        ;;
esac
