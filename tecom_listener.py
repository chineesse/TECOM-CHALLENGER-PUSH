#!/usr/bin/env python3
"""
Tecom / Challenger TCP listener with Pushover notifications and
configurable rules (YAML).

- Listens for Computer Event Driven / Printer format on TCP.
- Stores all events in events.log.
- Queues new events in events_queue.txt.
- Background thread processes queue:
    * applies notification rules
    * sends to Pushover (if enabled)
    * removes successfully sent items from queue

Config: see config.yaml
"""

import os
import time
import socket
import threading
import re
import requests
import yaml
from typing import Dict, Any, List, Tuple

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")


# ==========================
# CONFIG LOAD
# ==========================

def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Basic sanity defaults
    cfg.setdefault("server", {})
    cfg["server"].setdefault("listen_host", "0.0.0.0")
    cfg["server"].setdefault("listen_port", 5000)
    cfg["server"].setdefault("poll_interval_seconds", 5)
    cfg["server"].setdefault("base_dir", "/var/lib/tecom-listener")

    cfg.setdefault("pushover", {})
    cfg["pushover"].setdefault("enabled", False)
    cfg["pushover"].setdefault("user_key", "")
    cfg["pushover"].setdefault("api_token", "")
    cfg["pushover"].setdefault("default_priority", 0)

    cfg.setdefault("zones", {})
    cfg.setdefault("inputs", {})
    cfg.setdefault("notifications", [])

    return cfg


config = load_config(CONFIG_PATH)

BASE_DIR = config["server"]["base_dir"]
QUEUE_FILE = os.path.join(BASE_DIR, "events_queue.txt")
LOG_FILE = os.path.join(BASE_DIR, "events.log")

LISTEN_HOST = config["server"]["listen_host"]
LISTEN_PORT = int(config["server"]["listen_port"])
POLL_INTERVAL_SECONDS = int(config["server"]["poll_interval_seconds"])

ZONES: Dict[str, str] = config.get("zones", {})
INPUTS: Dict[str, str] = config.get("inputs", {})

PUSHOVER_ENABLED = bool(config["pushover"]["enabled"])
PUSHOVER_USER_KEY = config["pushover"]["user_key"]
PUSHOVER_API_TOKEN = config["pushover"]["api_token"]
PUSHOVER_DEFAULT_PRIORITY = int(config["pushover"]["default_priority"])

NOTIFICATION_RULES: List[Dict[str, Any]] = config.get("notifications", [])

os.makedirs(BASE_DIR, exist_ok=True)
file_lock = threading.Lock()


# ==========================
# LOGGING & QUEUE
# ==========================

def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    line = f"{now_ts()} {message}"
    with file_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(line)


def queue_event(raw_event: str) -> None:
    with file_lock:
        with open(QUEUE_FILE, "a", encoding="utf-8") as f:
            f.write(raw_event + "\n")


# ==========================
# PARSING & RULE ENGINE
# ==========================

ZONE_RE = re.compile(r"ZONE\s+(\d+)")
INPUT_RE = re.compile(r"INPUT\s+(\d+)")


def extract_context(raw_line: str) -> Dict[str, Any]:
    """
    Pull out zone/input numbers & names for template rendering.
    """
    ctx: Dict[str, Any] = {
        "raw": raw_line,
        "zone_number": "",
        "zone_name": "",
        "input_number": "",
        "input_name": "",
    }

    m_zone = ZONE_RE.search(raw_line)
    if m_zone:
        zn = m_zone.group(1).zfill(3)
        ctx["zone_number"] = zn
        ctx["zone_name"] = ZONES.get(zn, "")

    m_input = INPUT_RE.search(raw_line)
    if m_input:
        inp = m_input.group(1).zfill(3)
        ctx["input_number"] = inp
        ctx["input_name"] = INPUTS.get(inp, "")

    return ctx


def render_template(template: str, ctx: Dict[str, Any]) -> str:
    """
    Safe-ish template render. Unknown keys become empty.
    """
    try:
        return template.format(**ctx)
    except KeyError:
        # If someone references a missing key, avoid crashing.
        return template


def choose_notification(raw_line: str) -> Tuple[bool, int, str, str]:
    """
    Apply notification rules from config.

    Returns: (send_pushover, priority, title, message)
    """
    ctx = extract_context(raw_line)

    for rule in NOTIFICATION_RULES:
        pattern = rule.get("match_regex", ".*")
        if not re.search(pattern, raw_line):
            continue

        send_p = bool(rule.get("send_pushover", True))
        priority = int(rule.get("priority", PUSHOVER_DEFAULT_PRIORITY))

        title_t = rule.get("title", "Challenger Event")
        msg_t = rule.get("message", "{raw}")

        title = render_template(title_t, ctx)
        message = render_template(msg_t, ctx)

        return send_p, priority, title, message

    # If no rules at all
    return True, PUSHOVER_DEFAULT_PRIORITY, "Challenger Event", raw_line


# ==========================
# PUSHOVER
# ==========================

def send_pushover(title: str, message: str, priority: int = 0) -> bool:
    if not PUSHOVER_ENABLED:
        log(f"Pushover disabled, not sending: {title} | {message}")
        return True  # treat as success so queue doesn't grow forever

    if not PUSHOVER_USER_KEY or not PUSHOVER_API_TOKEN:
        log("Pushover keys not configured, skipping send.")
        return False

    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_API_TOKEN,
                "user": PUSHOVER_USER_KEY,
                "title": title[:250],
                "message": message[:1024],
                "priority": priority,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        log(f"Pushover error {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        log(f"Pushover exception: {e}")
        return False


# ==========================
# QUEUE PROCESSOR
# ==========================

def process_queue_loop():
    log("Queue processor started")
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)

        with file_lock:
            if not os.path.exists(QUEUE_FILE):
                continue
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]

        if not lines:
            continue

        log(f"Processing {len(lines)} queued event(s)")
        remaining: List[str] = []

        for raw in lines:
            send_p, pri, title, msg = choose_notification(raw)
            if send_p:
                ok = send_pushover(title, msg, pri)
                if ok:
                    log(f"Sent notification: {title} | {raw}")
                else:
                    remaining.append(raw)
            else:
                log(f"Rule decided not to send notification: {raw}")

        with file_lock:
            with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                for r in remaining:
                    f.write(r + "\n")


# ==========================
# TCP SERVER
# ==========================

def handle_client(conn: socket.socket, addr):
    log(f"New connection from {addr[0]}:{addr[1]}")
    try:
        with conn:
            f = conn.makefile("r", encoding="utf-8", errors="ignore")
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event_line = f"{now_ts()} {line}"
                log(f"RX: {event_line}")
                queue_event(event_line)
    except Exception as e:
        log(f"Connection error from {addr[0]}: {e}")
    finally:
        log(f"Connection closed from {addr[0]}")


def server_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_HOST, LISTEN_PORT))
    sock.listen(5)
    log(f"Listening on {LISTEN_HOST}:{LISTEN_PORT}")

    while True:
        conn, addr = sock.accept()
        t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
        t.start()


def main():
    log("Tecom listener starting")
    t = threading.Thread(target=process_queue_loop, daemon=True)
    t.start()
    server_loop()


if __name__ == "__main__":
    main()
