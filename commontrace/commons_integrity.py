"""Authenticated curated-memory records with canonical, domain-separated HMAC.

Signing keys are deployment secrets distributed out of band. HMAC provides
integrity and authenticity within that trust group; verifiers possessing the
key can also sign. Unsigned records remain compatible unless policy requires
verification. This does not replace independent lesson review.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from typing import Any, cast

from commontrace.secrets_provider import env_secret

_FIELD = "_integrity"
_DOMAIN = "commontrace-commons-record-v1"
_MAX_BYTES = 4 * 1024 * 1024


class CommonsIntegrityError(ValueError):
    """An untrusted record failed authentication (never includes its contents)."""


def _key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) < 32:
        raise CommonsIntegrityError("commons verification requires a key of at least 32 bytes")
    return key


def _canonical(payload: Mapping[str, Any], key_id: str) -> bytes:
    try:
        wire = json.dumps(
            {"domain": _DOMAIN, "algorithm": "HMAC-SHA256", "key_id": key_id, "payload": dict(payload)},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise CommonsIntegrityError("commons record must contain finite UTF-8 JSON values") from None
    if len(wire) > _MAX_BYTES:
        raise CommonsIntegrityError("commons record exceeds authentication size limit")
    return wire


def sign_record(record: Mapping[str, Any], key: bytes, *, key_id: str = "default") -> dict[str, Any]:
    """Sign every record field, including unknown metadata and review provenance."""
    if _FIELD in record:
        raise CommonsIntegrityError("commons record already has authentication metadata")
    if not isinstance(key_id, str) or not key_id or len(key_id) > 128:
        raise CommonsIntegrityError("invalid commons authentication key identifier")
    wire = _canonical(record, key_id)
    # JSON round-trip isolates the returned record from caller mutation.
    payload = cast(dict[str, Any], json.loads(wire)["payload"])
    payload[_FIELD] = {"version": 1, "algorithm": "HMAC-SHA256", "key_id": key_id,
                       "signature": hmac.new(_key(key), wire, hashlib.sha256).hexdigest()}
    return payload


def verify_record(record: Mapping[str, Any], keys: Mapping[str, bytes], *, required: bool = False) -> bool:
    """Authenticate before storage/use; return False for permitted unsigned input."""
    metadata = record.get(_FIELD)
    if metadata is None:
        if required:
            raise CommonsIntegrityError("commons record is unsigned")
        return False
    if not isinstance(metadata, dict) or set(metadata) != {"version", "algorithm", "key_id", "signature"}:
        raise CommonsIntegrityError("malformed commons authentication metadata")
    key_id, signature = metadata.get("key_id"), metadata.get("signature")
    if (type(metadata.get("version")) is not int or metadata["version"] != 1
            or metadata.get("algorithm") != "HMAC-SHA256"
            or not isinstance(key_id, str) or not key_id or len(key_id) > 128
            or not isinstance(signature, str) or re.fullmatch(r"[0-9a-f]{64}", signature) is None):
        raise CommonsIntegrityError("unsupported commons authentication metadata")
    key = keys.get(key_id)
    if key is None:
        raise CommonsIntegrityError("commons authentication key is not trusted")
    payload = {name: value for name, value in record.items() if name != _FIELD}
    expected = hmac.new(_key(key), _canonical(payload, key_id), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise CommonsIntegrityError("commons record authentication failed")
    return True


def verification_policy() -> tuple[dict[str, bytes], bool]:
    """Resolve current and previous pinned keys afresh, supporting file rotation."""
    try:
        raw = env_secret("COMMONTRACE_COMMONS_VERIFY_KEY")
        previous = env_secret("COMMONTRACE_COMMONS_VERIFY_KEY_PREVIOUS")
    except (RuntimeError, UnicodeError):
        raise CommonsIntegrityError("commons verification secret source is unavailable") from None
    key_id = os.environ.get("COMMONTRACE_COMMONS_VERIFY_KEY_ID", "default")
    previous_id = os.environ.get("COMMONTRACE_COMMONS_VERIFY_KEY_PREVIOUS_ID", "previous")
    required = bool(raw or previous) or os.environ.get("COMMONTRACE_COMMONS_REQUIRE_SIGNED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    keys: dict[str, bytes] = {}
    if raw:
        keys[key_id] = _key(raw.encode("utf-8"))
    if previous:
        if previous_id in keys:
            raise CommonsIntegrityError("commons verification key identifiers must be distinct")
        keys[previous_id] = _key(previous.encode("utf-8"))
    if required and not keys:
        raise CommonsIntegrityError("commons verification is required but no trusted key is configured")
    return keys, required


def verify_records(records: list[dict[str, Any]]) -> None:
    """Verify a complete export before the caller performs any filesystem write."""
    keys, required = verification_policy()
    for record in records:
        if not isinstance(record, dict):
            raise CommonsIntegrityError("commons export contains a non-object record")
        # A signed record may be stored by a compatibility client without a key;
        # it is never represented as authenticated by this function.
        if keys or required:
            verify_record(record, keys, required=required)
