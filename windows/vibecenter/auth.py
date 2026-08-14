"""HMAC-SHA256 IPC authentication — byte-compatible with relay.py.

The canonical-JSON rules, token format, and field names match relay.py
exactly, so a relay signed on macOS verifies on Windows and vice versa.
Cross-compatibility is asserted in tests/test_ipc.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from typing import Any, Dict, Optional, Tuple

DEFAULT_TOKEN_FILE = "~/.vibe-island/run/ipc-token"
TOKEN_BYTES = 32


def token_file_path() -> str:
    return os.path.expanduser(
        os.environ.get("VIBE_ISLAND_IPC_TOKEN_FILE", DEFAULT_TOKEN_FILE)
    )


def load_or_create_token() -> bytes:
    path = token_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            token_hex = handle.read().strip()
        if len(token_hex) == TOKEN_BYTES * 2:
            return bytes.fromhex(token_hex)
    except OSError:
        pass
    token = os.urandom(TOKEN_BYTES)
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        handle.write(token.hex())
    os.replace(path + ".tmp", path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def canonical_json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sign_payload(payload: Dict[str, Any], token: bytes) -> Dict[str, Any]:
    signed = dict(payload)
    signed["auth_nonce"] = uuid.uuid4().hex
    unsigned = dict(signed)
    signed["auth_signature"] = hmac.new(
        token, canonical_json_bytes(unsigned), hashlib.sha256
    ).hexdigest()
    return signed


def verify_payload(payload: Dict[str, Any], token: bytes) -> Tuple[bool, str]:
    """Returns (ok, error). Fails closed on missing/invalid fields."""
    nonce = str(payload.get("auth_nonce") or "").strip()
    if not nonce:
        return False, "missing auth_nonce"
    signature = str(payload.get("auth_signature") or "").strip()
    if not signature:
        return False, "missing auth_signature"
    unsigned = dict(payload)
    unsigned.pop("auth_signature", None)
    expected = hmac.new(token, canonical_json_bytes(unsigned), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False, "invalid auth_signature"
    return True, ""
