#!/usr/bin/env python3
"""
vibe-island-usage-daemon — polls Z.ai usage and pushes to the notch app.

This is a long-running background process (unlike relay.py which is
short-lived per hook event). Every poll interval it:
  1. Reads the Z.ai API key from ~/.zcode/v2/config.json
  2. Calls open.bigmodel.cn/api/monitor/usage/quota/limit
  3. Parses 5h / 7d / monthly usage percentages
  4. Pushes {type: "usage", ...} to the notch app on 127.0.0.1:14321
  5. Pushes {type: "usage_status", ...} lifecycle updates

Usage:
  nohup python3 usage-daemon.py &> /tmp/vibe-usage.log &
"""
import fcntl
import json
import hashlib
import hmac
import os
import socket
import sys
import time
import glob
import urllib.error
import urllib.request
import uuid
import datetime
from typing import Any

HOST, PORT = "127.0.0.1", 14321
DEFAULT_POLL_INTERVAL = 120.0
DEFAULT_ZCODE_CONFIG = "~/.zcode/v2/config.json"
DEFAULT_LOCK_PATH = "~/.vibe-island/run/usage-daemon.lock"
DEFAULT_IPC_TOKEN_FILE = "~/.vibe-island/run/ipc-token"
QUOTA_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
SUBSCRIPTION_URL = "https://open.bigmodel.cn/api/biz/subscription/list"
CODEX_SESSIONS_DIR = os.environ.get(
    "VIBE_ISLAND_CODEX_SESSIONS_DIR",
    os.path.expanduser("~/.codex/sessions"),
)

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


def expand_path(path_value):
    return os.path.expanduser(path_value)


def zcode_config_path():
    return expand_path(os.environ.get("VIBE_ISLAND_USAGE_CONFIG", DEFAULT_ZCODE_CONFIG))


def lock_file_path():
    return expand_path(os.environ.get("VIBE_ISLAND_USAGE_LOCK", DEFAULT_LOCK_PATH))


def token_file_path():
    return expand_path(os.environ.get("VIBE_ISLAND_IPC_TOKEN_FILE", DEFAULT_IPC_TOKEN_FILE))


def poll_interval_seconds():
    raw_value = os.environ.get("VIBE_ISLAND_USAGE_POLL_INTERVAL")
    if not raw_value:
        return DEFAULT_POLL_INTERVAL
    try:
        interval = float(raw_value)
    except ValueError:
        return DEFAULT_POLL_INTERVAL
    return interval if interval > 0 else DEFAULT_POLL_INTERVAL


def canonical_json_bytes(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def load_token():
    path = token_file_path()
    try:
        with open(path, encoding="utf-8") as handle:
            token_hex = handle.read().strip()
    except FileNotFoundError as exc:
        raise ValueError(f"ipc token file missing: {path}") from exc
    except OSError as exc:
        raise ValueError(f"ipc token file unreadable: {exc}") from exc

    if len(token_hex) != 64:
        raise ValueError("ipc token file must contain exactly 64 hex characters")
    try:
        token = bytes.fromhex(token_hex)
    except ValueError as exc:
        raise ValueError("ipc token file must contain exactly 64 hex characters") from exc
    if len(token) != 32:
        raise ValueError("ipc token file must decode to 32 bytes")
    return token


def sign_payload(payload):
    signed_payload = dict(payload)
    signed_payload["auth_nonce"] = uuid.uuid4().hex
    unsigned_payload = dict(signed_payload)
    signed_payload["auth_signature"] = hmac.new(
        load_token(),
        canonical_json_bytes(unsigned_payload),
        hashlib.sha256,
    ).hexdigest()
    return signed_payload


def verify_payload(payload):
    nonce = str(payload.get("auth_nonce") or "").strip()
    if not nonce:
        return "missing auth_nonce in payload"

    signature = str(payload.get("auth_signature") or "").strip()
    if not signature:
        return "missing auth_signature in payload"

    unsigned_payload = dict(payload)
    unsigned_payload.pop("auth_signature", None)
    expected_signature = hmac.new(
        load_token(),
        canonical_json_bytes(unsigned_payload),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return "invalid auth_signature in payload"
    return None


def read_zai_key():
    """Extract the first non-empty bigmodel/zai API key from ZCode config."""
    try:
        with open(zcode_config_path(), encoding="utf-8") as handle:
            cfg = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    provs = cfg.get("provider", {})
    if not isinstance(provs, dict):
        return None
    priority = [
        "bigmodel-coding-plan",
        "bigmodel-start-plan",
        "zai-coding-plan",
        "zai-start-plan",
        "bigmodel",
        "zai",
    ]
    keys = {}
    for name, prov in provs.items():
        if not isinstance(prov, dict):
            continue
        key = prov.get("options", {}).get("apiKey", "")
        if not key:
            continue
        for preferred_name in priority:
            if preferred_name in name:
                keys.setdefault(preferred_name, key)
                break
    for preferred_name in priority:
        if preferred_name in keys:
            return keys[preferred_name]
    return None


def acquire_single_instance_lock(lock_path=None):
    target_path = expand_path(lock_path or lock_file_path())
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    handle = open(target_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    os.fsync(handle.fileno())
    return handle


def fetch_json(url, key):
    """GET url with Bearer auth, return parsed JSON or None."""
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "vibe-island-usage-daemon/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
        log(f"fetch error ({type(exc).__name__})")
        return None


def format_remaining(next_reset_ms):
    """Format ms-until-reset into a short human string."""
    if not next_reset_ms:
        return None
    now_ms = int(time.time() * 1000)
    seconds_remaining = max(0, (next_reset_ms - now_ms) // 1000)
    days = seconds_remaining // 86400
    hours = (seconds_remaining % 86400) // 3600
    minutes = (seconds_remaining % 3600) // 60
    if days > 0:
        return f"{days}d{hours}h"
    if hours > 0:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def parse_quota(resp):
    """Extract 5h/7d/monthly percentages + reset time from quota response."""
    if not resp or resp.get("code") != 200:
        return None
    data = resp.get("data", {})
    limits = data.get("limits", [])
    five_h = seven_d = monthly = None
    five_h_reset = seven_d_reset = monthly_reset = None
    for limit in limits:
        unit = limit.get("unit")
        percentage = limit.get("percentage")
        reset = limit.get("nextResetTime")
        if unit == UNIT_FIVE_HOUR:
            five_h = percentage
            five_h_reset = format_remaining(reset)
        elif unit == UNIT_SEVEN_DAY:
            seven_d = percentage
            seven_d_reset = format_remaining(reset)
        elif unit == UNIT_MONTHLY:
            monthly = percentage
            monthly_reset = format_remaining(reset)
    return {
        "five_hour": five_h,
        "five_hour_reset": five_h_reset,
        "seven_day": seven_d,
        "seven_day_reset": seven_d_reset,
        "monthly": monthly,
        "monthly_reset": monthly_reset,
        "level": data.get("level"),
    }


def parse_subscription(resp):
    """Extract plan name from the subscription API."""
    if not resp or resp.get("code") != 200:
        return None, None
    items = resp.get("data", [])
    if not items:
        return None, None
    for item in items:
        if item.get("status") == "VALID":
            return item.get("productName"), item.get("billingCycle")
    return items[0].get("productName"), items[0].get("billingCycle")


def build_usage_payload(snapshot):
    return {"type": "usage", "usage": snapshot}


def build_status_payload(state, detail=None):
    payload = {
        "type": "usage_status",
        "status": state,
    }
    if detail:
        payload["detail"] = detail
    return payload


def push_payload(payload):
    try:
        signed_payload = sign_payload(payload)
    except ValueError as exc:
        log(f"ipc auth unavailable ({exc})")
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect((HOST, PORT))
            sock.sendall((json.dumps(signed_payload) + "\n").encode("utf-8"))
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def push_usage(snapshot):
    return push_payload(build_usage_payload(snapshot))


def push_status(state, detail=None):
    return push_payload(build_status_payload(state, detail))


def poll_once(key):
    """One polling cycle: fetch + parse + push."""
    quota_resp = fetch_json(QUOTA_URL, key)
    if quota_resp is None:
        push_status("fetch_error", "quota_fetch_failed")
        return False

    usage = parse_quota(quota_resp)
    if usage is None:
        log("quota parse failed")
        push_status("fetch_error", "quota_parse_failed")
        return False

    subscription_resp = fetch_json(SUBSCRIPTION_URL, key)
    plan, cycle = parse_subscription(subscription_resp)
    usage["plan"] = plan
    usage["billing"] = cycle
    usage["provider"] = "Z.ai"

    pushed = push_usage(usage)
    push_status("ready")
    if pushed:
        log(
            f"pushed: 5h={usage.get('five_hour')}%/{usage.get('five_hour_reset')} "
            f"7d={usage.get('seven_day')}%/{usage.get('seven_day_reset')} "
            f"mo={usage.get('monthly')}%/{usage.get('monthly_reset')} "
            f"plan={plan}"
        )
    else:
        log("notch app not reachable, will retry")
    return pushed


def _format_reset_delta(resets_at_epoch, now_epoch=None):
    if not resets_at_epoch:
        return ""
    if now_epoch is None:
        now_epoch = int(time.time())
    remaining = max(0, int(resets_at_epoch) - now_epoch)
    if remaining <= 0:
        return ""
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    minutes = (remaining % 3600) // 60
    if days > 0:
        return f"{days}d{hours}h"
    if hours > 0:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def _extract_rate_limits(record):
    """Walk a rollout JSON line to find a rate_limits dict."""
    if not isinstance(record, dict):
        return None
    if "rate_limits" in record and isinstance(record["rate_limits"], dict):
        return record["rate_limits"]
    payload = record.get("payload")
    if isinstance(payload, dict):
        if "rate_limits" in payload and isinstance(payload["rate_limits"], dict):
            return payload["rate_limits"]
        state = payload.get("state")
        if isinstance(state, dict) and "rate_limits" in state:
            return state["rate_limits"]
    return None


def read_codex_usage():
    """Read the latest rate_limits from Codex session rollout files.

    The Codex CLI writes its rate-limit status into every rollout file
    as part of world_state events. We read the most recently modified
    rollout that has a non-null primary rate limit and extract the
    used_percent / resets_at for the weekly window.
    """
    pattern = os.path.join(CODEX_SESSIONS_DIR, "**", "rollout-*.jsonl")
    try:
        candidates = sorted(
            glob.glob(pattern, recursive=True),
            key=lambda p: os.path.getmtime(p),
            reverse=True,
        )
    except OSError:
        return None

    now_epoch = int(time.time())
    best_rate = None
    for path in candidates[:8]:
        try:
            if now_epoch - os.path.getmtime(path) > 86400:
                continue
        except OSError:
            continue
        try:
            with open(path) as fh:
                for line in fh:
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    rate = _extract_rate_limits(record)
                    if rate and rate.get("primary"):
                        best_rate = rate
        except OSError:
            continue
        if best_rate:
            break

    if not best_rate or not best_rate.get("primary"):
        return None

    primary = best_rate["primary"]
    used = int(round(primary.get("used_percent", 0)))
    reset_str = _format_reset_delta(primary.get("resets_at"), now_epoch)
    credits = best_rate.get("credits") or {}
    unlimited = bool(credits.get("unlimited"))
    plan = "Unlimited" if unlimited else "Codex"

    return {
        "provider": "Codex",
        "seven_day": used,
        "seven_day_reset": reset_str,
        "plan": plan,
        "level": "max" if used >= 80 else ("high" if used >= 50 else "low"),
    }


def poll_codex_usage():
    """Read Codex usage from local rollout files and push to the app."""
    usage = read_codex_usage()
    if usage is None:
        return False
    pushed = push_usage(usage)
    if pushed:
        log(f"pushed codex: 7d={usage.get('seven_day')}%/{usage.get('seven_day_reset')} plan={usage.get('plan')}")
    return pushed


def _strip_api_suffix(base_url):
    """Normalise a provider baseURL to its root for billing endpoints."""
    url = base_url.rstrip("/")
    for suffix in ["/anthropic", "/v1/openai", "/v1/chat/completions",
                   "/api/v1/zcode-plan/anthropic", "/zcode-plan/anthropic"]:
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    url = url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def discover_zcode_providers():
    """Return all enabled providers with an API key from ZCode config."""
    try:
        with open(zcode_config_path(), encoding="utf-8") as handle:
            cfg = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    provs = cfg.get("provider", {})
    if not isinstance(provs, dict):
        return
    seen_keys = set()
    for pid, prov in provs.items():
        if not isinstance(prov, dict):
            continue
        if prov.get("systemDisabledReason"):
            continue
        opts = prov.get("options", {})
        key = opts.get("apiKey", "")
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        base_url = opts.get("baseURL", "")
        name = prov.get("name", pid)[:20]
        yield {
            "id": pid,
            "name": name,
            "key": key,
            "base_url": base_url,
            "strategy_hint": _classify_provider(pid, base_url),
        }


def _classify_provider(pid, base_url):
    """Heuristic: which strategy to try first for this provider."""
    combined = f"{pid} {base_url}".lower()
    if "bigmodel" in combined or "open.bigmodel.cn" in combined or ".z.ai" in combined:
        return "bigmodel"
    return "openai_compat"


def fetch_openai_billing_usage(base_url, key):
    """Try OpenAI-compatible billing endpoints (new-api / one-api / OpenRouter)."""
    root = _strip_api_suffix(base_url)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "vibe-island-usage-daemon/1.0",
    }
    now = datetime.date.today()
    start = now.replace(day=1).isoformat()
    end = (now + datetime.timedelta(days=1)).isoformat()

    sub_url = f"{root}/v1/dashboard/billing/subscription"
    try:
        req = urllib.request.Request(sub_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            sub = json.loads(resp.read().decode("utf-8"))
    except Exception:
        sub = None
    hard_limit = None
    if isinstance(sub, dict):
        hard_limit = sub.get("hard_limit_usd") or sub.get("system_hard_limit_usd")

    usage_url = f"{root}/v1/dashboard/billing/usage?start_date={start}&end_date={end}"
    try:
        req = urllib.request.Request(usage_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            usage_resp = json.loads(resp.read().decode("utf-8"))
    except Exception:
        usage_resp = None
    total_usage = None
    if isinstance(usage_resp, dict):
        total_usage = usage_resp.get("total_usage")

    if hard_limit is None and total_usage is None:
        return _fetch_newapi_usage(root, key, headers)

    used_usd = (total_usage or 0) / 100.0
    if hard_limit and hard_limit > 0:
        pct = min(100, round(used_usd / hard_limit * 100))
    elif total_usage is not None:
        pct = round(used_usd)
    else:
        pct = 0

    return {
        "monthly": pct,
        "monthly_reset": None,
        "plan": f"${hard_limit:.2f}" if hard_limit else "Pay-as-you-go",
        "level": "max" if pct >= 80 else ("high" if pct >= 50 else "low"),
    }


def _fetch_newapi_usage(root, key, headers):
    """Try new-api /one-api style /api/user/self endpoint."""
    url = f"{root}/api/user/self"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    data = body.get("data", {}) if isinstance(body, dict) else {}
    quota = data.get("quota")
    used = data.get("used_quota")
    if quota is None and used is None:
        return None
    quota = quota or 1
    used = used or 0
    pct = min(100, round(used / quota * 100)) if quota > 0 else 0
    return {
        "monthly": pct,
        "monthly_reset": None,
        "plan": data.get("group") or "Token Plan",
        "level": "max" if pct >= 80 else ("high" if pct >= 50 else "low"),
    }


def _poll_bigmodel_for_key(key, name):
    """Fetch BigModel/Z.ai quota using the proprietary API."""
    quota_resp = fetch_json(QUOTA_URL, key)
    if quota_resp is None:
        return None
    usage = parse_quota(quota_resp)
    if usage is None:
        return None
    sub_resp = fetch_json(SUBSCRIPTION_URL, key)
    plan, _ = parse_subscription(sub_resp)
    usage["plan"] = plan or name
    return usage


def poll_all_providers():
    """Discover and poll every enabled provider in ZCode config."""
    polled_any = False
    for prov in discover_zcode_providers():
        snapshot = None
        hint = prov["strategy_hint"]
        if hint == "bigmodel":
            snapshot = _poll_bigmodel_for_key(prov["key"], prov["name"])
        else:
            snapshot = fetch_openai_billing_usage(prov["base_url"], prov["key"])
        if snapshot:
            snapshot["provider"] = prov["name"]
            if push_usage(snapshot):
                pct = snapshot.get("monthly") or snapshot.get("seven_day") or 0
                log(f"pushed {prov['name']}: {pct}% plan={snapshot.get('plan', '?')}")
                polled_any = True
        else:
            log(f"no usage data for {prov['name']} ({hint})")
    return polled_any


def main():
    lock_handle = acquire_single_instance_lock()
    if lock_handle is None:
        log("another usage-daemon instance is already running")
        push_status("already_running")
        return 0

    try:
        log("usage-daemon started")
        push_status("starting")

        interval = poll_interval_seconds()
        first_cycle = True
        while True:
            # Self-terminate if the notch app that launched us is gone
            # (reparented to launchd). Covers graceful quit, SIGTERM, and
            # crash alike so no polling orphan outlives the app.
            if os.getppid() == 1:
                log("parent process exited, stopping daemon")
                return 0

            # Codex usage from local rollout files (no API key needed).
            poll_codex_usage()

            # Discover and poll every enabled provider in ZCode config.
            # Covers BigModel, Z.ai, and any OpenAI-compatible token plan.
            polled = poll_all_providers()
            if polled:
                push_status("ready")
            else:
                key = read_zai_key()
                if not key:
                    push_status("unconfigured")
                else:
                    push_status("fetch_error", "no provider returned usage data")

            if not first_cycle:
                time.sleep(interval)
            first_cycle = False
    finally:
        lock_handle.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("stopped by user")
