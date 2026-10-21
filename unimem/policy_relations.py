"""Optional fail-closed OpenFGA and SpiceDB retrieval admission.

Enrollment is local operator-only and immutable. Provider state, schema and
availability remain external assumptions; no finite-policy proof covers them.
No network is used without explicit enrollment. Credentials stay in the secret
provider, and only authenticated application identities select subjects.
"""
from __future__ import annotations

import functools
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

from unimem import frontier_protocol as fp

CONFIG = "memory-relation-provider.json"
MAX_IDS = 8192
MAX_BYTES = 1024*1024
NAMESPACES = {"facts", "traces", "lessons", "records"}
_LOOKUP: ContextVar[dict[tuple[str, str, str], frozenset[str]]] = ContextVar("unimem_relation_lookup", default={})
Transport = Callable[[str, dict[str, Any], str], bytes]


def _normalize(config: Mapping[str, Any]) -> dict[str, Any]:
    fp.wire(config)
    if set(config) != {"version", "provider", "endpoint", "store_id", "model_id", "types", "relation",
                       "subjects", "credential_env"} or type(config["version"]) is not int or config["version"] != 1:
        raise ValueError("UniMem expected an explicit versioned relationship provider configuration")
    if config["provider"] not in {"openfga", "spicedb"}:
        raise ValueError("UniMem unknown relationship provider")
    parsed = urlsplit(fp.text(config["endpoint"], "relationship endpoint", 2048))
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("UniMem relationship endpoint must use HTTPS without inline credentials or query")
    for key in ("relation", "credential_env"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", config[key]):
            raise ValueError("UniMem invalid relationship permission or secret reference")
    types = config["types"]
    if not isinstance(types, dict) or not types or set(types)-NAMESPACES:
        raise ValueError("UniMem relationship namespaces must be explicit canonical retrieval namespaces")
    for value in types.values():
        if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,61}[a-z0-9]", value):
            raise ValueError("UniMem invalid relationship resource type")
    subjects = config["subjects"]
    if not isinstance(subjects, dict) or not 1 <= len(subjects) <= 32:
        raise ValueError("UniMem expected bounded authenticated principal-to-subject mappings")
    for principal, subject in subjects.items():
        fp.text(principal, "authenticated relationship principal", 128)
        if not isinstance(subject, dict) or set(subject) != {"type", "id"}:
            raise ValueError("UniMem relationship subjects need an explicit type and id")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,61}[a-z0-9]", subject["type"]):
            raise ValueError("UniMem invalid relationship subject type")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", subject["id"]):
            raise ValueError("UniMem invalid relationship subject id")
    if config["provider"] == "openfga":
        for key in ("store_id", "model_id"):
            if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", config[key]):
                raise ValueError("UniMem OpenFGA requires pinned store and authorization model IDs")
    elif config["store_id"] or config["model_id"]:
        raise ValueError("UniMem SpiceDB has no OpenFGA store or model id")
    return dict(json.loads(fp.wire(config)))


def configure(root: str, config: Mapping[str, Any]) -> dict[str, Any]:
    from unimem import _jsonl, nextwave_store, transparency_log

    nextwave_store.operator()
    clean = _normalize(config)
    filename = fp.filename(root, CONFIG)
    with _jsonl.locked(filename):
        if os.path.exists(filename):
            if load(root) != clean:
                raise PermissionError("UniMem relationship enrollment is immutable; use a new reviewed realm")
            return clean
        signed = fp.sign(root, clean)
        anchor = transparency_log.append(root, "memory-relation-enrollment", {"digest": fp.digest(signed)})
        _jsonl.write_json(filename, {"configuration": signed, "anchor": anchor})
    return clean


def load(root: str) -> dict[str, Any] | None:
    from unimem import transparency_log

    filename = fp.filename(root, CONFIG)
    try:
        if os.path.islink(filename):
            raise PermissionError("UniMem relationship configuration cannot be a symlink")
        with open(filename, "rb") as stream:
            raw = stream.read(128*1024+1)
    except FileNotFoundError:
        return None
    if len(raw) > 128*1024:
        raise PermissionError("UniMem relationship configuration exceeds its bound")
    envelope = json.loads(raw)
    signed, anchor = envelope["configuration"], envelope["anchor"]
    if (not fp.verify(root, signed) or signed.get("authority") != "operator"
            or not transparency_log.verify(root, anchor)
            or anchor["statement"]["record"]["event"] != "memory-relation-enrollment"
            or anchor["statement"]["record"]["statement"].get("digest") != fp.digest(signed)):
        raise PermissionError("UniMem relationship configuration signature or enrollment does not match")
    return _normalize(signed["record"])


def enabled(root: str, namespace: str) -> bool:
    if not os.path.lexists(fp.filename(root, CONFIG)):
        return False
    config = load(root)
    return config is not None and namespace in config["types"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _transport(url: str, body: dict[str, Any], credential_env: str) -> bytes:
    from unimem.secrets_provider import env_secret

    secret = env_secret(credential_env)
    if not secret:
        raise PermissionError("UniMem relationship provider credential is unavailable")
    request = urllib.request.Request(url, data=fp.wire(body), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer "+secret})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=2) as response:
            raw = response.read(MAX_BYTES+1)
        if len(raw) > MAX_BYTES:
            raise PermissionError("UniMem relationship response exceeds its bound")
        return bytes(raw)
    except (OSError, urllib.error.URLError):
        raise PermissionError("UniMem relationship provider is unavailable; retrieval denied") from None


def lookup(config: Mapping[str, Any], namespace: str, principal: str, *,
           transport: Transport | None = None) -> frozenset[str]:
    """Execute real wire contracts; injected transports are for trusted adapters/tests."""
    clean = _normalize(config)
    subject = clean["subjects"].get(principal)
    if subject is None:
        return frozenset()
    kind = clean["types"][namespace]
    endpoint = clean["endpoint"].rstrip("/")
    if clean["provider"] == "openfga":
        url = endpoint+"/stores/"+clean["store_id"]+"/list-objects"
        body = {"authorization_model_id": clean["model_id"], "type": kind,
                "relation": clean["relation"], "user": subject["type"]+":"+subject["id"],
                "consistency": "HIGHER_CONSISTENCY"}
    else:
        url = endpoint+"/v1/permissions/resources"
        body = {"consistency": {"fully_consistent": True}, "resource_object_type": kind,
                "permission": clean["relation"], "subject": {"object": {
                    "object_type": subject["type"], "object_id": subject["id"]}}, "optional_limit": MAX_IDS+1}
    raw = (transport or _transport)(url, body, clean["credential_env"])
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise PermissionError("UniMem relationship response exceeds its bound")
    try:
        if clean["provider"] == "openfga":
            response = json.loads(raw)
            if (not isinstance(response, dict) or set(response) != {"objects"}
                    or not isinstance(response["objects"], list)):
                raise ValueError("invalid object response")
            rows = response["objects"]
            if any(not isinstance(row, str) or not row.startswith(kind+":") for row in rows):
                raise ValueError("mismatched resource type")
            identities = [row[len(kind)+1:] for row in rows]
        else:
            # grpc-gateway server streaming: one result envelope per JSON line.
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            identities = []
            for row in rows:
                result = row["result"]
                if ("error" in row or result["permissionship"] != "LOOKUP_PERMISSIONSHIP_HAS_PERMISSION"
                        or result.get("partial_caveat_info")):
                    raise ValueError("conditional or incomplete relationship result")
                identities.append(result["resource_object_id"])
        fp.count(len(identities), 0, MAX_IDS)
        for identity in identities:
            fp.text(identity, "authorized relationship resource", 256)
        return frozenset(identities)
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise PermissionError("UniMem relationship provider returned invalid or incomplete authorization") from None


def pre_filter(root: str, namespace: str) -> frozenset[str] | None:
    if not enabled(root, namespace):
        return None
    from unimem import memory_authority

    principal, authority = memory_authority.bound_writer()
    actor = authority+":"+principal
    key = (os.path.realpath(root), namespace, actor)
    values = _LOOKUP.get().get(key)
    if values is None:
        config = load(root)
        if config is None:
            raise PermissionError("UniMem relationship enrollment disappeared during admission")
        values = lookup(config, namespace, actor)
    return values


def authorized_ids(root: str, identities: Sequence[str], namespace: str) -> set[str]:
    values = pre_filter(root, namespace)
    return set(identities) if values is None else set(identities).intersection(values)


@contextmanager
def admission(root: str, namespace: str) -> Iterator[None]:
    """A single retrieval operation uses one fresh provider response, never a TTL cache."""
    if not enabled(root, namespace):
        yield
        return
    from unimem import memory_authority

    principal, authority = memory_authority.bound_writer()
    actor = authority+":"+principal
    config = load(root)
    if config is None:
        raise PermissionError("UniMem relationship enrollment disappeared during admission")
    key = (os.path.realpath(root), namespace, actor)
    if key in _LOOKUP.get():
        yield
        return
    token = _LOOKUP.set({**_LOOKUP.get(), key: lookup(config, namespace, actor)})
    try:
        yield
    finally:
        _LOOKUP.reset(token)


def scoped(namespace: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(function)
        def call(root: str, *args: Any, **kwargs: Any) -> Any:
            with admission(root, namespace):
                return function(root, *args, **kwargs)
        return call
    return decorate
