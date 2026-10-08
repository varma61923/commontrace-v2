"""Memory as a versioned filesystem: a store that is its own git repository gets
validation before every commit, a repair for merge conflicts in its append-only
files, and signed handoffs that let one agent pass a known memory state to another."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import sys
import time
from dataclasses import dataclass, field

from commontrace import memory_git, paths

CONFIG = "memfs.json"
DEFAULTS = {"max_file_bytes": 5 * 1024 * 1024, "max_files": 50_000, "max_lesson_chars": 20_000,
            "scan_secrets": True}
GITIGNORE = """# commontrace: derived or transient files
*.db-wal
*.db-shm
*.lock
attention/index.npz
attention/*.bin
embeddings-*.db
jobs.db
.handoff_key
.origin-key
attachments.jsonl
"""
KEY_FILE = ".handoff_key"
TEXT_SUFFIXES = (".md", ".jsonl", ".json", ".yaml", ".yml", ".txt")
_CONFLICT = re.compile(r"^<<<<<<< [^\n]*\n(.*?)^=======\n(.*?)^>>>>>>> [^\n]*\n?", re.S | re.M)


class MemfsError(RuntimeError):
    """A memory filesystem operation that cannot proceed."""


def config(root: str) -> dict:
    path = os.path.join(paths.memory_dir(root), CONFIG)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return dict(DEFAULTS)
    except (OSError, ValueError) as exc:
        raise MemfsError(f"{path}: {exc}") from None
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def _git(root: str, *args: str, check: bool = True) -> str:
    ok, out, err = memory_git._run_git(list(args), root)
    if not ok and check:
        raise MemfsError(err.strip() or f"git {' '.join(args)} failed")
    return out


def require_repo(root: str) -> None:
    if not memory_git.owns_repo(root):
        raise MemfsError("this store is not its own git repository; run `commontrace memory init` "
                         "(or `commontrace init --git`) first")


def init(root: str) -> dict:
    """Make the store its own repository, ignore derived files, install the hook."""
    if memory_git.is_repo(root) and not memory_git.owns_repo(root):
        raise MemfsError("the store is inside another git repository; keep it in its own directory")
    status = memory_git.init_repo(root)
    if not status.get("ok"):
        raise MemfsError(status.get("error", "git init failed"))
    ignore = os.path.join(root, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as fh:
            fh.write(GITIGNORE)
    hook = install_hook(root)
    commit = memory_git.commit_all(root, "commontrace: version memory")
    return {"root": root, "hook": hook, "committed": commit.get("committed", False)}


def install_hook(root: str) -> str:
    """A pre-commit hook that validates staged memory files and refuses a bad commit."""
    hooks = _git(root, "rev-parse", "--git-path", "hooks").strip()
    if not os.path.isabs(hooks):
        hooks = os.path.join(root, hooks)
    os.makedirs(hooks, exist_ok=True)
    path = os.path.join(hooks, "pre-commit")
    prefix = shlex.quote(sys.executable)
    if not getattr(sys, "frozen", False):
        prefix += " -m commontrace.cli"
    script = ("#!/bin/sh\n# installed by commontrace: validate memory before it is committed\n"
              f"exec {prefix} memory validate --staged --dest {shlex.quote(os.path.abspath(root))}\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(script)
    os.chmod(path, 0o755)  # nosec B103 - a git hook must be executable
    return path


# --- validation -------------------------------------------------------------------

@dataclass
class Report:
    checked: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _memory_files(root: str) -> list[str]:
    base = paths.memory_dir(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        out += [os.path.join(dirpath, f) for f in filenames if not f.startswith(".")]
    return out


def _staged(root: str) -> list[str]:
    names = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACM").splitlines()
    return [os.path.join(root, n) for n in names if n.strip()]


def validate(root: str, files: list[str] | None = None) -> Report:
    """Check memory files against the store's limits: sizes, counts, parseable JSONL
    and lesson frontmatter, conflict markers, and credentials in text files."""
    from commontrace import frontmatter, memory_guard

    cfg = config(root)
    report = Report()
    every = _memory_files(root)
    if len(every) > cfg["max_files"]:
        report.problems.append(f"memory/ holds {len(every)} files; the limit is {cfg['max_files']}")
    for path in (every if files is None else files):
        if not os.path.isfile(path):
            continue
        rel = os.path.relpath(path, root)
        report.checked += 1
        size = os.path.getsize(path)
        if size > cfg["max_file_bytes"]:
            report.problems.append(f"{rel}: {size} bytes; the limit is {cfg['max_file_bytes']}")
            continue
        if not path.endswith(TEXT_SUFFIXES):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            report.problems.append(f"{rel}: unreadable ({exc})")
            continue
        if _CONFLICT.search(text):
            report.problems.append(f"{rel}: unresolved merge conflict (run `commontrace memory repair`)")
            continue
        if path.endswith(".jsonl"):
            for n, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    try:
                        json.loads(line)
                    except ValueError:
                        report.problems.append(f"{rel}:{n}: not a JSON object")
                        break
        elif path.endswith(".json"):
            try:
                json.loads(text)
            except ValueError as exc:
                report.problems.append(f"{rel}: invalid JSON ({exc})")
        elif path.endswith(".md") and os.sep + "lessons" + os.sep in path:
            try:
                fm, body = frontmatter.read(path)
            except frontmatter.FrontmatterError as exc:
                report.problems.append(f"{rel}: {exc}")
                continue
            if len(body) > cfg["max_lesson_chars"]:
                report.problems.append(f"{rel}: lesson body {len(body)} chars; the limit is "
                                       f"{cfg['max_lesson_chars']}")
        if cfg["scan_secrets"]:
            high = [f for f in memory_guard.scan_text(text, rel)
                    if f.category == memory_guard.CATEGORY_SECRET and f.blocking]
            if high:
                report.problems.append(f"{rel}: looks like it contains a credential ({high[0].label})")
    return report


# --- history -------------------------------------------------------------------------

def status(root: str) -> dict:
    require_repo(root)
    lines = [ln for ln in _git(root, "status", "--porcelain", "--", ".").splitlines() if ln.strip()]
    return {"head": memory_git.head_hash(root), "changed": [ln[3:] for ln in lines]}


def commit(root: str, message: str, *, verify: bool = True) -> dict:
    require_repo(root)
    if verify:
        report = validate(root)
        if not report.ok:
            raise MemfsError("validation failed:\n  " + "\n  ".join(report.problems))
        result = memory_git.commit_all(root, message)
        if not result.get("ok"):
            raise MemfsError(result.get("error", "git commit failed"))
        return result
    _git(root, "add", "-A", "--", ".")
    if not _git(root, "status", "--porcelain", "--", ".").strip():
        return {"ok": True, "committed": False, "reason": "clean"}
    _git(root, "commit", "-q", "--no-verify", "-m", (message or "").strip() or "commontrace: snapshot")
    return {"ok": True, "committed": True, "commit": memory_git.head_hash(root)}


def diff(root: str, rev: str | None = None) -> str:
    require_repo(root)
    if rev and not re.fullmatch(r"[A-Za-z0-9._/~^-]{1,80}", rev):
        raise MemfsError(f"not a revision: {rev!r}")
    return _git(root, "diff", "--stat", "--patch", *( [rev] if rev else []), "--", "memory")


def restore(root: str, rev: str) -> dict:
    """Put memory/ back to how it was at `rev`, as a new commit; history is kept."""
    require_repo(root)
    if not re.fullmatch(r"[A-Za-z0-9._/~^-]{1,80}", rev or ""):
        raise MemfsError(f"not a revision: {rev!r}")
    full = _git(root, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()
    _git(root, "restore", "--source", full, "--staged", "--worktree", "--", "memory")
    return memory_git.commit_all(root, f"commontrace: restore memory to {full[:12]}")


# --- conflict repair -------------------------------------------------------------------

def _merge_jsonl(ours: str, theirs: str) -> str:
    rows, order = {}, []
    for line in ours.splitlines() + theirs.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        key = (obj.get("id") or obj.get("key")) if isinstance(obj, dict) and (obj.get("id") or obj.get("key")) \
            else line
        if key not in rows:
            order.append(key)
        rows[key] = line
    return "\n".join(rows[k] for k in order) + "\n"


def repair(root: str) -> dict:
    """Resolve merge conflicts in memory files. Append-only JSONL takes the union of
    both sides (a row with the same id keeps the later side); any other file keeps
    our side and parks theirs beside it as <name>.theirs for review."""
    repaired, parked, resolved = [], [], []
    for path in _memory_files(root):
        if not path.endswith(TEXT_SUFFIXES):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        if not _CONFLICT.search(text):
            continue
        rel = os.path.relpath(path, root)
        if path.endswith(".jsonl"):
            merged = _CONFLICT.sub(lambda m: _merge_jsonl(m.group(1), m.group(2)), text)
            merged = _merge_jsonl(merged, "")
            repaired.append(rel)
            resolved.append(rel)
        else:
            merged = _CONFLICT.sub(lambda m: m.group(1), text)
            theirs = _CONFLICT.sub(lambda m: m.group(2), text)
            with open(path + ".theirs", "w", encoding="utf-8") as fh:
                fh.write(theirs)
            parked.append(rel + ".theirs")
            resolved.append(rel)
        tmp = path + ".repair"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(merged)
        os.replace(tmp, path)
    if memory_git.owns_repo(root) and resolved:
        _git(root, "add", "--", *resolved, check=False)
    return {"repaired": repaired, "parked": parked}


# --- signed handoff --------------------------------------------------------------------

def _key(root: str) -> bytes:
    env = os.environ.get("COMMONTRACE_HANDOFF_KEY", "").strip()
    if env:
        if len(env) < 32:
            raise MemfsError("COMMONTRACE_HANDOFF_KEY must be at least 32 characters")
        return env.encode()
    path = os.path.join(paths.memory_dir(root), KEY_FILE)
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
    except FileNotFoundError:
        raise MemfsError("no handoff key: set COMMONTRACE_HANDOFF_KEY or run `commontrace memory keygen`") from None
    if len(key) < 32:
        raise MemfsError(f"{path}: the key is too short")
    return key


def keygen(root: str, *, force: bool = False) -> str:
    path = os.path.join(paths.memory_dir(root), KEY_FILE)
    if os.path.exists(path) and not force:
        raise MemfsError(f"{path} exists; pass --force to replace it (outstanding handoffs stop verifying)")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_urlsafe(48))
    return path


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _tree_digest(root: str, rev: str, paths_: list[str]) -> str:
    out = _git(root, "ls-tree", "-r", rev, "--", *paths_)
    return hashlib.sha256(out.encode()).hexdigest()


def handoff(root: str, *, audience: str, issuer: str = "", ttl_seconds: int = 3600,
            scope: list[str] | None = None) -> str:
    """A token naming a committed memory state that `audience` may take over: the commit,
    a digest of the files in scope, who issued it and until when it holds."""
    require_repo(root)
    if not re.fullmatch(r"[A-Za-z0-9._:@-]{1,128}", audience or ""):
        raise MemfsError("audience must be 1-128 characters of letters, digits and . _ : @ -")
    pending = status(root)["changed"]
    if pending:
        raise MemfsError(f"{len(pending)} uncommitted change(s); commit memory before handing it off")
    scope = scope or ["memory"]
    commit_id = memory_git.head_hash(root)
    if not commit_id:
        raise MemfsError("memory has no commits yet; run `commontrace memory commit` first")
    now = int(time.time())
    claims = {"v": 1, "commit": commit_id, "scope": scope, "tree": _tree_digest(root, commit_id, scope),
              "aud": audience, "iss": issuer or os.environ.get("USER", "commontrace"), "iat": now,
              "exp": now + max(60, min(int(ttl_seconds), 30 * 86400)), "nonce": secrets.token_hex(8)}
    body = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_key(root), body.encode(), hashlib.sha256).digest())
    return f"cth1.{body}.{sig}"


def verify_handoff(root: str, token: str, *, audience: str | None = None) -> dict:
    """Check a handoff: signature, expiry, audience, that the commit exists here, and
    whether memory has moved on since (drift)."""
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != "cth1":
        raise MemfsError("not a handoff token")
    expected = _b64(hmac.new(_key(root), parts[1].encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, parts[2]):
        raise MemfsError("signature does not verify (wrong key, or the token was altered)")
    try:
        claims = json.loads(_unb64(parts[1]))
    except ValueError:
        raise MemfsError("malformed token body") from None
    if claims.get("exp", 0) < time.time():
        raise MemfsError("the handoff has expired")
    if audience is not None and claims.get("aud") != audience:
        raise MemfsError(f"the handoff is for {claims.get('aud')!r}, not {audience!r}")
    require_repo(root)
    _git(root, "cat-file", "-e", f"{claims['commit']}^{{commit}}")
    if _tree_digest(root, claims["commit"], claims["scope"]) != claims["tree"]:
        raise MemfsError("the commit's files do not match what was handed off")
    head = memory_git.head_hash(root) or claims["commit"]
    drift = head != claims["commit"] and bool(_git(root, "diff", "--name-only", claims["commit"], head, "--",
                                                   *claims["scope"]).strip())
    return {**claims, "head": head, "drift": drift}


def sign_snapshot(root: str, *, issuer: str) -> dict:
    """Detached HMAC attestation of a committed memory tree, stored in git metadata."""
    from commontrace import _jsonl

    require_repo(root)
    commit_id = memory_git.head_hash(root)
    if not commit_id or status(root)["changed"]:
        raise MemfsError("commit memory before signing its snapshot")
    claims = {"version": 1, "commit": commit_id, "tree": _tree_digest(root, commit_id, ["memory"]),
              "issuer": issuer, "scope": ["memory"]}
    signature = hmac.new(_key(root), json.dumps(claims, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    row = {**claims, "signature": signature}
    filename = _git(root, "rev-parse", "--git-path", "commontrace-attestations").strip()
    if not os.path.isabs(filename):
        filename = os.path.join(root, filename)
    _jsonl.write_json(os.path.join(filename, commit_id + ".json"), row)
    return row


def verify_snapshot(root: str, attestation: dict) -> bool:
    try:
        claims = {k: v for k, v in attestation.items() if k != "signature"}
        signature = hmac.new(_key(root), json.dumps(claims, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        return (hmac.compare_digest(signature, attestation["signature"])
                and _tree_digest(root, claims["commit"], claims["scope"]) == claims["tree"])
    except (KeyError, ValueError, MemfsError):
        return False


def attach(root: str, shared_root: str, *, agent_id: str, token: str) -> dict:
    """Attach a read-only shared repository using an audience-bound handoff."""
    from commontrace import _jsonl

    shared_root = os.path.realpath(shared_root)
    claims = verify_handoff(shared_root, token, audience=agent_id)
    if claims["drift"] or status(shared_root)["changed"] or claims["scope"] != ["memory"]:
        raise MemfsError("shared memory changed; create a fresh handoff")
    path = os.path.join(paths.memory_dir(root), "attachments.jsonl")
    row = {"shared_root": shared_root, "agent_id": agent_id, "handoff": token, "commit": claims["commit"]}
    with _jsonl.locked(path):
        _jsonl.append_row(path, row)
    os.chmod(path, 0o600)
    return {k: v for k, v in row.items() if k != "handoff"}


def attached_roots(root: str, *, agent_id: str) -> list[str]:
    from commontrace import _jsonl

    latest = {row["shared_root"]: row for row in _jsonl.read_rows(
        os.path.join(paths.memory_dir(root), "attachments.jsonl")) if row["agent_id"] == agent_id}
    result = []
    for shared_root, row in latest.items():
        claims = verify_handoff(shared_root, row["handoff"], audience=agent_id)
        if claims["drift"] or status(shared_root)["changed"] or claims["scope"] != ["memory"]:
            raise MemfsError("shared memory attachment drifted; refresh its handoff")
        result.append(shared_root)
    return result
