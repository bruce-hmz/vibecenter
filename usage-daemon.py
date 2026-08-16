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
import json
import hashlib
import hmac
import os
import re
import socket
import sys
import time
import glob
import urllib.error
import urllib.parse
import urllib.request
import uuid
import datetime
from typing import Any

try:
    import fcntl
except ImportError:  # Windows — use the CRT byte-range lock instead.
    fcntl = None
    import msvcrt

HOST, PORT = "127.0.0.1", 14321
DEFAULT_POLL_INTERVAL = 120.0
DEFAULT_ZCODE_CONFIG = "~/.zcode/v2/config.json"
DEFAULT_LOCK_PATH = "~/.vibe-island/run/usage-daemon.lock"
DEFAULT_IPC_TOKEN_FILE = "~/.vibe-island/run/ipc-token"
DEFAULT_LOG_FILE = "~/.vibe-island/run/usage-daemon.log"
QUOTA_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
SUBSCRIPTION_URL = "https://open.bigmodel.cn/api/biz/subscription/list"
CODEX_SESSIONS_DIR = os.environ.get(
    "VIBE_ISLAND_CODEX_SESSIONS_DIR",
    os.path.expanduser("~/.codex/sessions"),
)

# Google AI plan (Gemini CLI OAuth login) — same HEALTH_CHECK probe the
# CLI's /about command uses to surface tier + Google One AI credits. The
# OAuth client credentials are gemini-cli's own public embedded values;
# rather than committing them (secret scanners rightly object), they are
# discovered at runtime from the installed gemini-cli package, or supplied
# via env vars / a config file.
GEMINI_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
CLOUDCODE_LOAD_URL = (
    "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
)
DEFAULT_GEMINI_CREDS_PATH = "~/.gemini/oauth_creds.json"
G1_CREDIT_TYPE = "GOOGLE_ONE_AI"
GEMINI_OAUTH_CONFIG = "~/.vibe-island/gemini-oauth.json"
# Global npm-style install roots where @google/gemini-cli/bundle/*.js may
# live (npm root -g locations across platforms).
GEMINI_CLI_BUNDLE_DIRS = [
    "~/AppData/Roaming/npm/node_modules/@google/gemini-cli/bundle",
    "~/.npm-global/lib/node_modules/@google/gemini-cli/bundle",
    "~/.local/share/../lib/node_modules/@google/gemini-cli/bundle",
    "/usr/local/lib/node_modules/@google/gemini-cli/bundle",
    "/usr/lib/node_modules/@google/gemini-cli/bundle",
]

_gemini_oauth_client_cache = {}

# unit codes returned by the quota API (reverse-engineered from real app logs):
#   3 → 5-hour rolling window
#   6 → 7-day rolling window
#   5 → monthly tools (TIME_LIMIT)
UNIT_FIVE_HOUR = 3
UNIT_SEVEN_DAY = 6
UNIT_MONTHLY = 5


def log(msg):
    """Log to stdout and a persistent file so launched-daemon output is never lost."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        log_file = expand_path(os.environ.get("VIBE_ISLAND_USAGE_LOG", DEFAULT_LOG_FILE))
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


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
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                return None
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
    # The quota itself is a rolling 7-day window (10080 minutes), and the
    # CLI often leaves rate_limits null in the currently-active rollout —
    # the freshest real value can easily live in a file untouched for days.
    # So the file-mtime cutoff must match the quota window, and we prefer
    # a record whose reset timestamp is still in the future.
    max_file_age = 7 * 86400
    fallback_rate = None
    best_rate = None
    for path in candidates[:24]:
        try:
            if now_epoch - os.path.getmtime(path) > max_file_age:
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
                        reset_at = rate["primary"].get("resets_at") or 0
                        if reset_at > now_epoch:
                            best_rate = rate
                        else:
                            fallback_rate = fallback_rate or rate
        except OSError:
            continue
        if best_rate:
            break
    best_rate = best_rate or fallback_rate

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


# ── Google AI plan (Gemini CLI login) ────────────────────
# Reads ~/.gemini/oauth_creds.json (written by `gemini` on login), refreshes
# the access token with gemini-cli's public OAuth client, then issues the
# same loadCodeAssist HEALTH_CHECK the CLI's /about command uses. Yields the
# plan tier and the Google One AI credit balance for paid plans.

_gemini_token_cache = {"access_token": None, "expiry_date": 0}


def gemini_creds_path():
    return expand_path(
        os.environ.get("VIBE_ISLAND_GEMINI_OAUTH_CREDS", DEFAULT_GEMINI_CREDS_PATH)
    )


def read_gemini_creds():
    try:
        with open(gemini_creds_path(), "r", encoding="utf-8") as handle:
            creds = json.load(handle)
        return creds if isinstance(creds, dict) and creds.get("refresh_token") else None
    except (OSError, ValueError):
        return None


def http_post_json(url, payload, headers=None, form=False):
    """POST helper shared by the Gemini poller (mocked in tests)."""
    if form:
        data = urllib.parse.urlencode(payload).encode()
    else:
        data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type",
                       "application/x-www-form-urlencoded" if form
                       else "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def discover_gemini_oauth_client():
    """(client_id, client_secret) for gemini-cli's public OAuth app.

    Resolution order: env override → ~/.vibe-island/gemini-oauth.json →
    scanning the installed gemini-cli bundle (the pair appears adjacent as
    OAUTH_CLIENT_ID/OAUTH_CLIENT_SECRET string literals).
    """
    if _gemini_oauth_client_cache:
        return _gemini_oauth_client_cache
    client = {
        "client_id": os.environ.get("VIBE_ISLAND_GEMINI_CLIENT_ID", ""),
        "client_secret": os.environ.get("VIBE_ISLAND_GEMINI_CLIENT_SECRET", ""),
    }
    if not (client["client_id"] and client["client_secret"]):
        try:
            with open(expand_path(GEMINI_OAUTH_CONFIG), encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                client["client_id"] = client["client_id"] or str(stored.get("client_id") or "")
                client["client_secret"] = client["client_secret"] or str(stored.get("client_secret") or "")
        except (OSError, ValueError):
            pass
    if not (client["client_id"] and client["client_secret"]):
        pattern = re.compile(
            r'([0-9]{8,}-[0-9a-z]{16,}\.apps\.googleusercontent\.com)'
            r'[^0-9G]{0,40}'
            r'(GOCSPX-[0-9A-Za-z_-]{16,})'
        )
        search_roots = GEMINI_CLI_BUNDLE_DIRS
        env_roots = os.environ.get("VIBE_ISLAND_GEMINI_CLI_DIRS", "")
        if env_roots:
            search_roots = [part for part in env_roots.split(":") if part]
        for root in search_roots:
            bundle_dir = expand_path(root)
            if not os.path.isdir(bundle_dir):
                continue
            for name in sorted(glob.glob(os.path.join(bundle_dir, "*.js"))):
                try:
                    with open(name, encoding="utf-8", errors="replace") as fh:
                        found = pattern.search(fh.read())
                except OSError:
                    continue
                if found:
                    client = {"client_id": found.group(1),
                              "client_secret": found.group(2)}
                    break
            if client["client_id"]:
                break
    _gemini_oauth_client_cache.update(client)
    return _gemini_oauth_client_cache


def refresh_gemini_token(creds):
    """Exchange the refresh token for a fresh access token.

    Writes the updated credentials back to oauth_creds.json (best effort —
    gemini-cli itself refreshes in place, so this keeps both in sync).
    """
    client = discover_gemini_oauth_client()
    if not (client.get("client_id") and client.get("client_secret")):
        log("gemini plan: OAuth client unknown — set VIBE_ISLAND_GEMINI_"
            "CLIENT_ID/SECRET or install gemini-cli")
        return None
    form = {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": creds.get("refresh_token"),
        "grant_type": "refresh_token",
    }
    token_resp = http_post_json(GEMINI_OAUTH_TOKEN_URL, form, form=True)
    access_token = token_resp.get("access_token")
    if not access_token:
        return None
    creds = dict(creds)
    creds["access_token"] = access_token
    creds["expiry_date"] = token_resp.get("expires_in", 3600) * 1000 + int(
        time.time() * 1000
    )
    try:
        with open(gemini_creds_path(), "w", encoding="utf-8") as handle:
            json.dump(creds, handle, indent=2)
    except OSError:
        pass
    return access_token


def gemini_access_token():
    """Cached access token, refreshed when missing or about to expire."""
    now_ms = int(time.time() * 1000)
    cached = _gemini_token_cache
    if cached["access_token"] and cached["expiry_date"] > now_ms + 60_000:
        return cached["access_token"]
    creds = read_gemini_creds()
    if not creds:
        return None
    if creds.get("access_token") and creds.get("expiry_date", 0) > now_ms + 60_000:
        cached["access_token"] = creds["access_token"]
        cached["expiry_date"] = creds["expiry_date"]
        return cached["access_token"]
    token = refresh_gemini_token(creds)
    if token:
        cached["access_token"] = token
        cached["expiry_date"] = now_ms + 3_600_000
    return token


def cloudcode_health_check(access_token):
    """loadCodeAssist in HEALTH_CHECK mode — tier + credit wallet."""
    payload = {
        "metadata": {
            "ideType": "IDE_UNSPECIFIED",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
        },
        "mode": "HEALTH_CHECK",
    }
    try:
        return http_post_json(
            CLOUDCODE_LOAD_URL, payload,
            headers={"Authorization": f"Bearer {access_token}"})
    except (urllib.error.URLError, OSError, ValueError):
        return None


GEMINI_PLAN_LABELS = {
    "free-tier": "Gemini Free",
    "standard-tier": "Google AI Pro",
    "legacy-paid-tier": "Google AI Ultra",
}


def gemini_snapshot_from_response(resp):
    """Build a usage snapshot from the loadCodeAssist response."""
    tier = resp.get("currentTier") or {}
    tier_id = str(tier.get("id") or "")
    snapshot = {
        "provider": "Gemini",
        "plan": GEMINI_PLAN_LABELS.get(tier_id, tier_id or "Gemini"),
        "level": tier_id,
    }
    credits_total = 0
    saw_credits = False
    paid = resp.get("paidTier") or {}
    for credit in paid.get("availableCredits") or []:
        if not isinstance(credit, dict):
            continue
        if str(credit.get("creditType") or "") != G1_CREDIT_TYPE:
            continue
        saw_credits = True
        try:
            credits_total += int(float(str(credit.get("creditAmount") or 0)))
        except (TypeError, ValueError):
            continue
    if saw_credits:
        snapshot["credits"] = f"{credits_total:,}"
    return snapshot


def poll_gemini_plan():
    """Poll the Google AI plan and push a signed usage snapshot."""
    if read_gemini_creds() is None:
        return None  # gemini CLI never logged in on this machine
    token = gemini_access_token()
    if not token:
        log("gemini plan: token refresh failed")
        return None
    resp = cloudcode_health_check(token)
    if not resp:
        log("gemini plan: loadCodeAssist failed")
        return None
    snapshot = gemini_snapshot_from_response(resp)
    if push_usage(snapshot):
        log(f"pushed Gemini plan: {snapshot.get('plan')} "
            f"credits={snapshot.get('credits', '-')}")
        return snapshot
    return None


def poll_codex_usage():
    """Read Codex usage from local rollout files and push to the app."""
    usage = read_codex_usage()
    if usage is None:
        return False
    pushed = push_usage(usage)
    if pushed:
        log(f"pushed codex: 7d={usage.get('seven_day')}%/{usage.get('seven_day_reset')} plan={usage.get('plan')}")
    return pushed


# ── OpenCode Go plan (via the opencodex service) ────────
# opencodex (the local agent gateway) tracks the OpenCode Go plan quota in
# ~/.opencodex/codex-quota-cache.json and refreshes it from upstream while
# the service runs. We surface the freshest entry: weekly percent, reset
# time, and — when the plan tracks credits instead — the balance.

OPENCODEX_QUOTA_CACHE = "~/.opencodex/codex-quota-cache.json"
OPENCODE_QUOTA_MAX_AGE = 7 * 86400 * 1000  # ms; matches the weekly window


def read_opencode_quota():
    path = expand_path(
        os.environ.get("VIBE_ISLAND_OPENCODEX_QUOTA_CACHE", OPENCODEX_QUOTA_CACHE)
    )
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    quotas = data.get("quotas") if isinstance(data, dict) else None
    if not isinstance(quotas, dict) or not quotas:
        return None
    newest = None
    for entry in quotas.values():
        if not isinstance(entry, dict):
            continue
        if newest is None or (entry.get("updatedAt") or 0) > (newest.get("updatedAt") or 0):
            newest = entry
    if newest is None:
        return None
    now_ms = int(time.time() * 1000)
    if now_ms - (newest.get("updatedAt") or 0) > OPENCODE_QUOTA_MAX_AGE:
        return None  # service gone / plan idle past the window
    snapshot = {
        "provider": "OpenCode",
        "plan": "Go",
    }
    weekly = newest.get("weeklyPercent")
    if isinstance(weekly, (int, float)):
        used = int(round(weekly))
        snapshot["seven_day"] = used
        snapshot["seven_day_reset"] = _format_reset_delta(
            newest.get("weeklyResetAt"), int(time.time()))
        snapshot["level"] = "max" if used >= 80 else ("high" if used >= 50 else "low")
    try:
        credits = int(newest.get("resetCredits") or 0)
    except (TypeError, ValueError):
        credits = 0
    if credits > 0 and "seven_day" not in snapshot:
        snapshot["credits"] = f"{credits:,}"
    if "seven_day" not in snapshot and "credits" not in snapshot:
        return None
    return snapshot


def poll_opencode_go():
    """Push the OpenCode Go plan quota, when opencodex tracks one."""
    snapshot = read_opencode_quota()
    if snapshot is None:
        return None
    if push_usage(snapshot):
        log(f"pushed opencode go: {snapshot.get('seven_day')}%/"
            f"{snapshot.get('seven_day_reset', '-')} credits={snapshot.get('credits', '-')}")
        return snapshot
    return None


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
            snapshot["provider"] = "智谱 GLM" if hint == "bigmodel" else prov["name"]
            if push_usage(snapshot):
                pct = snapshot.get("monthly") or snapshot.get("seven_day") or 0
                log(f"pushed {snapshot.get('provider', prov['name'])}: {pct}% plan={snapshot.get('plan', '?')}")
                polled_any = True
            else:
                log(f"push failed for {prov['name']} (notch app unreachable on {HOST}:{PORT})")
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

            # Google AI plan (Gemini CLI login): tier + Google One AI credits.
            poll_gemini_plan()

            # OpenCode Go plan (via the opencodex service quota cache).
            poll_opencode_go()

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
