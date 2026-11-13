"""Write-time origin binding and action-specific filtering for governed facts.

The store operator owns the local key. Text and scope labels cannot nominate
their authority; a gateway supplies the authenticated writer context instead.
Derived facts inherit the minimum trust of their authenticated source records.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from unimem import _jsonl, capabilities, origin, paths

TRUST = {"external": 0, "tool": 1, "agent": 1, "user": 2, "operator": 3}
WRITER: ContextVar[tuple[str, str]] = ContextVar("unimem_memory_writer", default=("local", "operator"))


def is_operator() -> bool:
    """Administrative authority belongs to the undelegated store operator.

    A capability narrows primary authentication even when the enclosing service
    writer is an operator or its leaf has the highest integrity ceiling.
    """
    return capabilities.CURRENT.get() is None and WRITER.get()[1] == "operator"


def bound_writer() -> tuple[str, str]:
    principal, authority = WRITER.get()
    value = capabilities.CURRENT.get()
    if value is None:
        return principal, authority
    from unimem import capability_io

    capability_io.current(value["root"])
    rank = min(TRUST[authority], TRUST[value["authority"]], value["verified"]["caveats"]["ceiling"])
    bounded = next(name for name in ("external", "agent", "user", "operator") if TRUST[name] == rank)
    return value["principal"], bounded


@contextmanager
def writer(principal: str, authority: str):
    if authority not in TRUST or not isinstance(principal, str) or not principal:
        raise ValueError("invalid authenticated writer")
    if capabilities.CURRENT.get() is not None:
        actual_principal, actual_authority = bound_writer()
        principal = actual_principal
        authority = min((authority, actual_authority), key=lambda name: TRUST[name])
    token = WRITER.set((principal, authority))
    try:
        yield
    finally:
        WRITER.reset(token)


def _key(root: str, *, create: bool = False, algorithm: str = "hmac-sha256", durable: bool = True) -> bytes | None:
    filename = os.path.join(paths.memory_dir(root), ".origin-ed25519-key" if algorithm == "ed25519" else ".origin-key")
    paths.enforce_boundary(root, filename)
    if create and not os.path.lexists(filename):
        paths.safe_prepare_output_path(filename)
        with _jsonl.locked(filename):
            try:
                fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(secrets.token_bytes(32))
                    fh.flush()
                    if durable:
                        os.fsync(fh.fileno())
    try:
        if os.path.islink(filename):
            raise ValueError("origin key must not be a symlink")
        # Validate the opened descriptor rather than a path checked before open.
        # Nonblocking mode makes special files fail without waiting for a writer.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(filename, flags)
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("origin key must be a regular file without hardlinks")
            if hasattr(os, "getuid") and info.st_uid != os.getuid() or os.name != "nt" and info.st_mode & 0o077:
                raise ValueError("origin key requires private owner-only access")
            key = fh.read(33)
    except FileNotFoundError:
        return None
    if len(key) != 32:
        if create:
            # Another first signer may have reserved the exclusive file but
            # not flushed its bytes yet. Wait for its creation lock and retry
            # once without creating or repairing an invalid existing key.
            with _jsonl.locked(filename):
                return _key(root, algorithm=algorithm)
        raise ValueError("invalid origin key")
    return key


def trace_record(trace: dict, *, context: str | None = None, solution: str | None = None) -> dict:
    """Bind content and metadata that determine evidence eligibility and outcomes."""
    record = {
        "id": trace.get("id"),
        "context": trace.get("context_text", "") if context is None else context,
        "solution": trace.get("solution_text", "") if solution is None else solution,
        "scopes": trace.get("scopes", []),
        "outcome": trace.get("outcome", {}),
        "expires": trace.get("expires"),
        "expires_at": trace.get("expires_at"),
        "valid_from": trace.get("valid_from"),
        "valid_until": trace.get("valid_until"),
        "procedure": trace.get("extensions", {}).get("profile", {}).get("procedure", {}),
    }
    if trace.get("source_traces"):
        record["source_traces"] = trace["source_traces"]
    return record


def fact_record(fact) -> dict:
    return {
        name: getattr(fact, name)
        for name in (
            "id",
            "statement",
            "category",
            "scopes",
            "valid_from",
            "valid_until",
            "expires_at",
            "source_traces",
            "memory_type",
        )
    }


def signing_config(root: str) -> dict:
    filename = os.path.join(paths.memory_dir(root), "origin-signing.json")
    try:
        paths.enforce_boundary(root, filename)
        with open(filename, encoding="utf-8") as fh:
            config = json.load(fh)
        if not isinstance(config, dict) or config.get("algorithm") not in ("ed25519", "hmac-sha256"):
            raise ValueError("invalid store signing configuration")
        if "public_key" in config and len(bytes.fromhex(config["public_key"])) != 32:
            raise ValueError("invalid pinned Ed25519 public key")
        return config
    except FileNotFoundError:
        return {"algorithm": "hmac-sha256"}


def configure_signing(root: str, *, algorithm: str = "ed25519") -> dict:
    if not is_operator():
        raise PermissionError("only an operator can configure store signing")
    if algorithm not in ("ed25519", "hmac-sha256"):
        raise ValueError("unsupported signing algorithm")
    config = {**signing_config(root), "algorithm": algorithm}
    if algorithm == "ed25519":
        key = _key(root, create="public_key" not in config, algorithm=algorithm)
        if key is None:
            raise PermissionError("configured Ed25519 private key is missing")
        config["public_key"] = origin.ed25519_public_bytes(key).hex()
    _jsonl.write_json(os.path.join(paths.memory_dir(root), "origin-signing.json"), config)
    return config


def verify(root: str, receipt: dict, record: dict | None = None) -> bool:
    if receipt.get("authority") not in TRUST:
        return False
    algorithm = receipt.get("algorithm", "hmac-sha256")
    if algorithm == "ed25519":
        try:
            public = bytes.fromhex(signing_config(root)["public_key"])
        except (ValueError, KeyError):
            return False
        principal = origin.Principal(
            receipt.get("principal", ""), "local-store", receipt["authority"], b"", "ed25519", public
        )
    else:
        key = _key(root)
        if key is None:
            return False
        principal = origin.Principal(receipt.get("principal", ""), "local-store", receipt["authority"], key)
    expected = receipt.get("record")
    matches = record is None or expected == record
    # Earlier v2 trace receipts did not bind an empty source list. Admit only
    # that exact legacy omission; adding a parent still invalidates its signature.
    if (
        not matches
        and record is not None
        and isinstance(expected, dict)
        and record_class(expected) == "trace"
        and not expected.get("source_traces")
        and not record.get("source_traces")
    ):
        matches = {k: v for k, v in expected.items() if k != "source_traces"} == {
            k: v for k, v in record.items() if k != "source_traces"
        }
    return origin.verify(receipt, {principal.id: principal}) and matches


def bind(root: str, record: dict, *, sources: list[str] | None = None, _redact_history: bool = False) -> dict:
    from unimem import safety

    if _redact_history and not is_operator():
        raise PermissionError("only the store operator can replace redacted origin history")
    if record_class(record) != "journal":
        from unimem import policy_io

        policy_io.assert_write(root, record)
    if record.get("id"):
        safety.assert_mutable(root, [record["id"]])
    principal_id, authority = bound_writer()
    if capabilities.CURRENT.get() is not None and record_class(record) != "journal":
        from unimem import capability_io

        effective = capability_io.before_write(root, record)
        if effective:
            principal_id, authority = effective
    algorithm = signing_config(root)["algorithm"]
    config = signing_config(root)
    key = _key(root, create=algorithm != "ed25519" or "public_key" not in config, algorithm=algorithm)
    if key is None:
        raise PermissionError("this public-verifier store has no signing key")
    if algorithm == "ed25519":
        public = origin.ed25519_public_bytes(key).hex()
        if config.get("public_key") != public:
            raise PermissionError("private signing key differs from the pinned public key")
    ledger = os.path.join(paths.memory_dir(root), "origins.jsonl")
    with _jsonl.locked(ledger):
        wanted = set(sources or []) | {record.get("id")}
        prior = {
            r["record"].get("id"): r
            for r in _jsonl.read_rows(ledger)
            if r.get("record", {}).get("id") in wanted and verify(root, r)
        }
        before = prior.get(record.get("id"))
        if before and record_class(before["record"]) != record_class(record):
            raise ValueError("origin record id is already bound to a different record kind")
        if before:
            authority = min((authority, before["authority"]), key=lambda name: TRUST[name])
        inherited = [prior.get(source) for source in sources or []]
        if record_class(record) != "journal":
            _check_source_scopes(record.get("scopes", []), [r["record"].get("scopes", []) for r in inherited if r])
        if inherited:
            ranks = [TRUST[r["authority"]] if r else 0 for r in inherited]
            minimum = min(TRUST[authority], *ranks)
            authority = next(name for name in ("external", "agent", "user", "operator") if TRUST[name] == minimum)
        signed = origin.bind(record, origin.Principal(principal_id, "local-store", authority, key, algorithm))
        if _redact_history:
            retained = [row for row in _jsonl.read_rows(ledger) if row.get("record", {}).get("id") != record.get("id")]
            _jsonl.write_rows(ledger, [*retained, signed])
        else:
            _jsonl.append_row(ledger, signed)
    if record.get("id"):
        from unimem import lineage

        lineage.attest(root, record, signed, inputs=sources or source_ids(record), operation=record_class(record))
        from unimem import transparency_log

        transparency_log.append(root, "write", {"id": record["id"], "content_hash": signed["digest"]}, receipt=False)
    return signed


def _check_source_scopes(derived: list[str], sources: list[list[str]]) -> None:
    """A derivation may narrow visibility, never broaden an authenticated owner scope."""

    def dimensions(labels):
        if not isinstance(labels, list) or len(labels) > 128:
            raise ValueError("scope labels must be a bounded list")
        groups = {}
        for label in labels:
            if not isinstance(label, str) or not 1 <= len(label) <= 256:
                raise ValueError("invalid scope label")
            dimension, _, value = label.partition(":")
            groups.setdefault(dimension, set()).add(value)
        return groups

    wanted = dimensions(derived)
    for source in sources:
        for dimension, owners in dimensions(source).items():
            if wanted.get(dimension) != owners:
                raise PermissionError("derived memory must preserve every authenticated source owner scope")


def permits(root: str, fact, *, action_class: str = "") -> bool:
    return permits_record(root, fact.origin, fact_record(fact), action_class=action_class)


def permits_record(root: str, receipt: dict, record: dict, *, action_class: str = "") -> bool:
    from unimem import interference

    if capabilities.CURRENT.get() is not None:
        from unimem import capability_io

        if not capability_io.permits(root, record):
            return False
    if not interference.read_allowed(root, record.get("id", "")):
        return False
    if lineage_blocked(root, record.get("id", "")):
        return False
    if not receipt and previously_bound(root, record.get("id", "")):
        return False  # Removing a receipt is not a migration back to unsigned legacy data.
    if receipt and not verify(root, receipt, record):
        return False
    from unimem import policy_io, sycophancy_io

    if not policy_io.permits(root, record):
        return False
    from unimem import policy_relations

    namespace = {"fact": "facts", "trace": "traces"}.get(record_class(record))
    if namespace and record.get("id") not in policy_relations.authorized_ids(root, [record.get("id", "")], namespace):
        return False
    if not sycophancy_io.read_allowed(root, record, action_class):
        return False
    if not action_class:
        return True
    import yaml

    filename = os.path.join(paths.memory_dir(root), "authority-policy.yaml")
    try:
        with open(filename, encoding="utf-8") as fh:
            policy = yaml.safe_load(fh)
    except FileNotFoundError:
        return True
    if not isinstance(policy, dict) or not isinstance(policy.get("actions"), dict):
        raise ValueError("authority policy requires an actions mapping")
    minimum = policy["actions"].get(action_class, policy.get("default", "operator"))
    if minimum not in TRUST:
        raise ValueError("unknown minimum origin authority")
    return TRUST.get(receipt.get("authority", "external"), 0) >= TRUST[minimum]


def previously_bound(root: str, record_id: str) -> bool:
    return any(
        row.get("record", {}).get("id") == record_id
        for row in _jsonl.read_rows(os.path.join(paths.memory_dir(root), "origins.jsonl"))
    )


def source_ids(record: dict) -> list[str]:
    return sorted(set([*record.get("source_traces", []), *record.get("data", {}).get("sources", [])]))


def lineage_blocked(root: str, record_id: str, *, canonical_only: bool = False) -> bool:
    return lineage_checker(root, canonical_only=canonical_only)(record_id)


def lineage_checker(root: str, *, canonical_only: bool = False) -> Callable[[str], bool]:
    """A reusable ``lineage_blocked`` for one batch of identities.

    ``lineage_blocked`` re-reads and signature-verifies forgetting.jsonl (and,
    once anything is forgotten, all of origins.jsonl) on every call, so a loop
    over N facts cost O(N * (F + O)) verifications -- quadratic, since origins
    grow with facts (1k facts with 1k origins: ~4 min). The checker loads and
    verifies each ledger at most once, on first need, so per-identity order of
    checks (sentinel, quarantine, then ledgers) and error behavior match.
    Use it within one operation; it snapshots the ledgers it has read.
    """
    memory = paths.memory_dir(root)
    forgetting_file = os.path.join(memory, "forgetting.jsonl")
    quarantine = not canonical_only and os.path.isfile(os.path.join(memory, "capabilities.json"))
    ledger: dict[str, Any] = {}

    def forgotten() -> dict[str, Any] | None:
        if "forgotten" not in ledger:
            if not os.path.isfile(forgetting_file):
                ledger["forgotten"] = None
            else:
                rows = {}
                for row in _jsonl.read_rows(forgetting_file):
                    record = {k: v for k, v in row.items() if k != "origin"}
                    if not verify(root, row.get("origin", {}), record):
                        raise PermissionError("forgetting receipt is invalid")
                    rows[row["target_id"]] = row["forgotten"]
                ledger["forgotten"] = rows if any(rows.values()) else None
        return ledger["forgotten"]

    def parents() -> dict[str, list[str]]:
        if "parents" not in ledger:
            ledger["parents"] = {
                r["record"].get("id"): source_ids(r["record"])
                for r in _jsonl.read_rows(os.path.join(memory, "origins.jsonl"))
                if verify(root, r)
            }
        return ledger["parents"]

    def blocked(record_id: str) -> bool:
        if not canonical_only:
            from unimem import sentinel_io

            if sentinel_io.blocked(root, record_id):
                return True
        if quarantine:
            from unimem import capability_io

            if capability_io.quarantined(root, record_id):
                return True
        targets = forgotten()
        if targets is None:
            return False
        graph = parents()
        pending, visited = [record_id], set()
        while pending:
            item = pending.pop()
            if targets.get(item):
                return True
            if item in visited:
                continue
            visited.add(item)
            pending.extend(graph.get(item, []))
        return False

    return blocked


def record_forgetting(root: str, record_id: str, *, forgotten: bool):
    if capabilities.CURRENT.get() is not None:
        from unimem import capability_io

        delegated = capability_io.current(root)
        if delegated is None or delegated["operation"] != ("forget" if forgotten else "write"):
            raise PermissionError("UniMem capability cannot change canonical forgetting authority")
    if forgotten:
        from unimem import incident, safety

        incident.assert_unpinned(root, [record_id])
        safety.assert_mutable(root, [record_id])
    path = os.path.join(paths.memory_dir(root), "forgetting.jsonl")
    with _jsonl.locked(path):
        sequence = len(_jsonl.read_rows(path))
        record = {"id": f"revocation:{sequence}", "target_id": record_id, "forgotten": forgotten, "sequence": sequence}
        _jsonl.append_row(path, {**record, "origin": bind(root, record)})


def provenance(root: str, record_id: str) -> dict:
    receipts = {}
    for row in _jsonl.read_rows(os.path.join(paths.memory_dir(root), "origins.jsonl")):
        if verify(root, row):
            receipts[row["record"].get("id")] = row
    missing: list[str] = []
    pending, visited, result = [record_id], set(), []
    while pending:
        item = pending.pop()
        if item in visited:
            continue
        visited.add(item)
        receipt = receipts.get(item)
        if receipt is None:
            missing.append(item)
            continue
        result.append(receipt)
        pending.extend(source_ids(receipt["record"]))
    return {
        "id": record_id,
        "receipts": result,
        "missing": sorted(missing),
        "complete": not missing,
        "blocked": lineage_blocked(root, record_id),
    }


def forgetting_certificate(root: str, record_id: str) -> dict:
    if not lineage_blocked(root, record_id):
        raise ValueError("record has no active forgetting revocation")
    ids = {
        row.get("record", {}).get("id")
        for row in _jsonl.read_rows(os.path.join(paths.memory_dir(root), "origins.jsonl"))
    }
    blocked = sorted(i for i in ids if isinstance(i, str) and lineage_blocked(root, i))
    record = {
        "id": "forget-certificate:" + record_id,
        "target_id": record_id,
        "kind": "local-forgetting-certificate",
        "blocked_records": blocked,
        "surfaces": ["fact-list", "evolution-search", "profile", "reflect", "derived-origin-gate"],
        "history_erased": False,
        "remote_erasure": False,
        "journal_sha256": __import__("hashlib")
        .sha256(origin._bytes({"events": _jsonl.read_rows(os.path.join(paths.memory_dir(root), "forgetting.jsonl"))}))
        .hexdigest(),
    }
    return {**record, "origin": bind(root, record)}


@contextmanager
def restricted_writer(principal: str, authority: str):
    current = WRITER.get()[1]
    if TRUST[current] < TRUST[authority]:
        authority = current
    with writer(principal, authority):
        yield


def record_class(record: dict) -> str:
    if record.get("record_kind") == "markdown-store":
        return "markdown-store"
    if "statement" in record and "category" in record:
        return "fact"
    if "context" in record and "solution" in record:
        return "trace"
    if "kind" in record and "data" in record and "revision" in record:
        return "control:" + record["kind"]
    return "journal"
