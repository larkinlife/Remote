#!/usr/bin/env python3
"""gcloud Auth Monitor: checks token health, alerts via Telegram once per 24h."""
import json
import os
import subprocess
import time
from pathlib import Path

ALERT_STATE_PATH = Path("/tmp/gcloud_auth_alert_state.json")
ALERT_COOLDOWN_S = 86400
BOT_ENV_PATH = Path.home() / ".config/tmate-telegram/bot.env"
CHAT_IDS_PATH = Path.home() / ".tmate-telegram/chat_ids.json"


def _load_alert_state():
    try:
        return json.loads(ALERT_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_alert_state(state):
    ALERT_STATE_PATH.write_text(json.dumps(state, indent=2))


def _send_telegram_alert(message):
    token = ""
    if BOT_ENV_PATH.exists():
        for line in BOT_ENV_PATH.read_text().splitlines():
            if line.startswith("TMATE_TELEGRAM_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break
    if not token:
        return False

    chat_ids = []
    try:
        chat_ids = json.loads(CHAT_IDS_PATH.read_text())
    except Exception:
        return False

    sent = False
    for chat_id in chat_ids:
        try:
            subprocess.run(
                ["curl", "-s", "-X", "POST",
                 "https://api.telegram.org/bot" + token + "/sendMessage",
                 "-d", "chat_id=" + str(chat_id),
                 "-d", "text=" + message,
                 "-d", "parse_mode=HTML"],
                capture_output=True, timeout=15)
            sent = True
        except Exception:
            pass
    return sent


def check_gcloud_auth_health(machines_config, self_id=""):
    accounts = set()
    for mid, m in machines_config.items():
        if mid != self_id:
            accounts.add(m.get("account", ""))
    accounts.discard("")

    state = _load_alert_state()
    now = time.time()
    results = {}

    for account in sorted(accounts):
        try:
            r = subprocess.run(
                ["gcloud", "auth", "print-access-token", "--account=" + account],
                capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip().startswith("ya29."):
                results[account] = "ok"
                if account in state:
                    del state[account]
                    _save_alert_state(state)
                continue
        except Exception:
            pass

        results[account] = "expired"

        last_alert = state.get(account, {}).get("last_alert", 0)
        if now - last_alert < ALERT_COOLDOWN_S:
            continue

        self_label = self_id or "unknown"
        message = (
            "<b>gcloud auth expired</b>\n\n"
            "Account: <code>" + account + "</code>\n"
            "Machine: <b>" + self_label + "</b>\n\n"
            "Alarm mesh cannot wake machines using this account.\n"
            "Re-auth: <code>gcloud auth login " + account + "</code>")
        _send_telegram_alert(message)

        state[account] = {"last_alert": now, "account": account}
        _save_alert_state(state)

    return results
