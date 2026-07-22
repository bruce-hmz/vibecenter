#!/usr/bin/env python3
"""
vibe-island-usage-daemon — polls Z.ai usage and pushes to the notch app.

This is a long-running background process (unlike relay.py which is
short-lived per hook event). Every POLL_INTERVAL seconds it:
  1. Reads the Z.ai API key from ~/.zcode/v2/config.json
  2. Calls open.bigmodel.cn/api/monitor/usage/quota/limit
  3. Parses 5h / 7d / monthly usage percentages
  4. Pushes {type: "usage", ...} to the notch app on 127.0.0.1:14321

The notch app stores the latest snapshot and renders it in compact mode.

Usage:
  nohup python3 usage-daemon.py &> /tmp/vibe-usage.log &
"""
import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error

HOST, PORT = "127.0.0.1", 14321
POLL_INTERVAL = 120  # seconds between polls
ZCODE_CONFIG = os.path.expanduser("~/.zcode/v2/config.json")
QUOTA_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
SUBSCRIPTION_URL = "https://open.bigmodel.cn/api/biz/subscription/list"

# unit codes returned by the quota API (reverse-engineered from real app logs):
#   3 → 5-hour rolling window
#   6 → 7-day rolling window
#   5 → monthly tools (TIME_LIMIT)
UNIT_FIVE_HOUR = 3
UNIT_SEVEN_DAY = 6
UNIT_MONTHLY = 5


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def read_zai_key():
    """Extract the first non-empty bigmodel/zai API key from ZCode config."""
    try:
        cfg = json.load(open(ZCODE_CONFIG))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    provs = cfg.get("provider", {})
    if not isinstance(provs, dict):
        return None
    # Prefer coding-plan keys (subscription-tier), fall back to others.
    priority = ["bigmodel-coding-plan", "bigmodel-start-plan",
                "zai-coding-plan", "zai-start-plan", "bigmodel", "zai"]
    keys = {}
    for name, prov in provs.items():
        if not isinstance(prov, dict):
            continue
        key = prov.get("options", {}).get("apiKey", "")
        if key:
            # match by substring (config key names vary)
            for p in priority:
                if p in name:
                    keys.setdefault(p, key)
                    break
    for p in priority:
        if p in keys:
            return keys[p]
    return None


def fetch_json(url, key):
    """GET url with Bearer auth, return parsed JSON or None."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "vibe-island-usage-daemon/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, TimeoutError) as e:
        log(f"  fetch error: {e}")
        return None


def format_remaining(next_reset_ms):
    """Format ms-until-reset into a short human string:
    5h window  → "53m"  (minutes)
    7d window  → "6d10h" (days+hours)
    monthly    → "16d10h"
    """
    if not next_reset_ms:
        return None
    now_ms = int(time.time() * 1000)
    sec = max(0, (next_reset_ms - now_ms) // 1000)
    days = sec // 86400
    hours = (sec % 86400) // 3600
    mins = (sec % 3600) // 60
    if days > 0:
        return f"{days}d{hours}h"
    if hours > 0:
        return f"{hours}h{mins}m"
    return f"{mins}m"


def parse_quota(resp):
    """Extract 5h/7d/monthly percentages + reset time from quota response."""
    if not resp or resp.get("code") != 200:
        return None
    data = resp.get("data", {})
    limits = data.get("limits", [])
    five_h = seven_d = monthly = None
    five_h_reset = seven_d_reset = monthly_reset = None
    for lim in limits:
        unit = lim.get("unit")
        pct = lim.get("percentage")
        reset = lim.get("nextResetTime")
        if unit == UNIT_FIVE_HOUR:
            five_h = pct
            five_h_reset = format_remaining(reset)
        elif unit == UNIT_SEVEN_DAY:
            seven_d = pct
            seven_d_reset = format_remaining(reset)
        elif unit == UNIT_MONTHLY:
            monthly = pct
            monthly_reset = format_remaining(reset)
    level = data.get("level")
    return {
        "five_hour": five_h,
        "five_hour_reset": five_h_reset,
        "seven_day": seven_d,
        "seven_day_reset": seven_d_reset,
        "monthly": monthly,
        "monthly_reset": monthly_reset,
        "level": level,
    }


def parse_subscription(resp):
    """Extract plan name from the subscription API."""
    if not resp or resp.get("code") != 200:
        return None, None
    items = resp.get("data", [])
    if not items:
        return None, None
    # pick the first VALID subscription
    for item in items:
        if item.get("status") == "VALID":
            return item.get("productName"), item.get("billingCycle")
    return items[0].get("productName"), items[0].get("billingCycle")


def push_usage(snapshot):
    """Send the usage snapshot to the notch app."""
    payload = {"type": "usage", "usage": snapshot}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect((HOST, PORT))
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def poll_once(key):
    """One polling cycle: fetch + parse + push. Returns True if pushed."""
    quota_resp = fetch_json(QUOTA_URL, key)
    if quota_resp is None:
        return False
    usage = parse_quota(quota_resp)
    if usage is None:
        log(f"  quota parse failed: {json.dumps(quota_resp)[:200]}")
        return False

    sub_resp = fetch_json(SUBSCRIPTION_URL, key)
    plan, cycle = parse_subscription(sub_resp)
    usage["plan"] = plan
    usage["billing"] = cycle
    # Tag with provider name so the notch UI can show "Z.ai" label and
    # support future multi-provider rotation.
    usage["provider"] = "Z.ai"

    pushed = push_usage(usage)
    if pushed:
        log(f"  pushed: 5h={usage.get('five_hour')}%/{usage.get('five_hour_reset')} "
            f"7d={usage.get('seven_day')}%/{usage.get('seven_day_reset')} "
            f"mo={usage.get('monthly')}%/{usage.get('monthly_reset')} "
            f"plan={plan}")
    else:
        log("  notch app not reachable, will retry")
    return pushed


def main():
    log("usage-daemon started")
    key = read_zai_key()
    if not key:
        log("ERROR: no Z.ai API key found in ~/.zcode/v2/config.json")
        log("  looked for: bigmodel-coding-plan / zai-coding-plan / etc.")
        sys.exit(1)
    log(f"key loaded ({len(key)} chars, prefix {key[:8]}...)")

    # first poll immediately
    poll_once(key)

    while True:
        time.sleep(POLL_INTERVAL)
        # Re-read key in case it was rotated
        new_key = read_zai_key()
        if new_key:
            key = new_key
        poll_once(key)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped by user")
