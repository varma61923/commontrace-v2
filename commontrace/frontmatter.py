"""Read/write Markdown files with YAML frontmatter (the CommonTrace file format)."""
from __future__ import annotations

import contextlib
import os
import re
import stat
import tempfile
import time
import uuid
from typing import Any

import yaml

try:
    import fcntl
except ImportError:  # pragma: no cover -- POSIX-only stdlib module
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover -- Windows-only stdlib module
    msvcrt = None  # type: ignore[assignment]

# Delimiter must be its own line (optionally trailing whitespace / CR), not just the
# substring "---" anywhere in the file -- a plain `content.split("---", 2)` corrupts
# any field whose value happens to contain "---" (e.g. a title like "before---after").
# \r is allowed so a raw CRLF string parses even when it didn't come from a
# universal-newline text-mode read.
_DELIM_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)


# Module scope, not a class attribute: a comprehension nested inside a class
# body gets its own scope and cannot see class-level names, so referencing
# this from the `yaml_implicit_resolvers` dict-comp below raises NameError at
# import time if it lives on the class.
_DROPPED_YAML_TAGS = frozenset({"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"})


class _StrictBoolLoader(yaml.SafeLoader):
    """SafeLoader, but the two YAML 1.1 implicit conversions that silently
    violate this protocol's own schemas are disabled.

    1. A bare `yes`/`no`/`on`/`off` (any case) resolves as the plain string
       it looks like, not a bool. Every field this protocol declares as a
       string -- agent_type, domain, tags entries, a lesson's applies_when
       -- is one PyYAML's default resolver can silently misparse:
       `domain: NO` or `tags: [on, off]` become {'domain': False} /
       {'tags': [True, False]} with no error, because YAML 1.1 treats those
       tokens as booleans.

    2. An unquoted ISO date or timestamp resolves as a string, not a
       `datetime.date`/`datetime.datetime`. `lesson.schema.json` declares
       `last_hit` as `"type": "string"`, and `trace.schema.json` declares
       `created_at` and `review_after` the same way -- so a file written
       the obvious way (`last_hit: 2026-07-01`, no quotes) loaded as a
       `date` object and then FAILED this project's own validator with
       "expected type string, got date". The repository's own shipped
       example lesson failed `commontrace lesson validate` out of the box
       because of this. Resolving to a string is the fix that matches the
       schema rather than loosening it: a field the protocol calls a string
       should be a string in memory, so downstream string operations work
       and `yaml.safe_dump` round-trips it unchanged.

    Both are only reachable from a *hand-edited* file: `commontrace`'s own
    write path (yaml.safe_dump) quotes what needs quoting. But this
    protocol explicitly relies on hand-editing (`lesson approve`/`reject`
    instruct manual edits), so the read path has to be the safe one.

    Rebuilds the resolver table on a SUBCLASS rather than mutating
    `yaml.SafeLoader.yaml_implicit_resolvers` in place: that dict is
    process-global, so patching it here would silently change bool and date
    resolution for every other `yaml.safe_load` call anywhere in the
    process (including third-party code), not just this module's reads.
    """

    yaml_implicit_resolvers = {
        first_char: [
            (tag, regexp) for tag, regexp in resolvers if tag not in _DROPPED_YAML_TAGS
        ]
        for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


# Re-added narrower than YAML 1.1's: only the six spellings YAML 1.2 core
# treats as booleans. `yes`/`no`/`on`/`off` fall through to plain strings.
_StrictBoolLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


class FrontmatterError(ValueError):
    """Raised when a file's frontmatter block is present but not parseable YAML,
    does not decode to a mapping, or the file cannot be opened at all.
    Callers should treat this as a clean, reportable error rather than letting
    a raw yaml.YAMLError/AttributeError/OSError traceback reach the user."""


def read(path: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body_markdown) for a `---\nYAML\n---\nbody` file."""
    # A path the user typed -- `lesson validate /nope/x.md`, or a directory
    # passed where a file was meant -- is a user error, not a crash. Every
    # other error path in this CLI prints "[commontrace] ..." and exits
    # non-zero; letting a raw FileNotFoundError/IsADirectoryError through
    # made this the odd one out.
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise FrontmatterError(f"cannot read {path}: {exc}") from exc
    if not content.startswith("---"):
        return {}, content
    delims = list(_DELIM_RE.finditer(content))
    if len(delims) < 2:
        return {}, content
    fm_text = content[delims[0].end():delims[1].start()]
    try:
        # bandit flags any yaml.load() call regardless of Loader, but
        # _StrictBoolLoader is a yaml.SafeLoader subclass (see its class
        # docstring above) that only narrows two implicit-conversion
        # rules -- it accepts no more of the YAML spec than SafeLoader
        # does, so this carries none of the arbitrary-object-instantiation
        # risk B506 exists to catch.
        fm = yaml.load(fm_text, Loader=_StrictBoolLoader)  # nosec B506
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"{path}: malformed YAML frontmatter: {exc}") from exc
    if fm is None:
        fm = {}
    if not isinstance(fm, dict):
        raise FrontmatterError(
            f"{path}: frontmatter must be a YAML mapping, got {type(fm).__name__}"
        )
    body = content[delims[1].end():].lstrip("\n")
    return fm, body


def _new_file_mode(target_dir: str) -> int:
    """The mode a brand-new file would get under the current process
    umask -- without the os.umask(0) / os.umask(restore) round-trip this
    replaced, which briefly sets the umask to 0 *process-wide*. Any OTHER
    thread that creates a file in that window (via this module or any
    other code running in the same process) gets one with no umask
    applied at all, i.e. world-writable -- a race across every thread in
    the process, not just this call.

    Instead, ask the kernel to apply the umask to a throwaway file: a
    single open() with O_CREAT combines the requested mode with the
    umask atomically, with no shared process state mutated in between.
    """
    probe_path = os.path.join(target_dir, f".commontrace-umask-probe-{uuid.uuid4().hex}")
    fd = os.open(probe_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    try:
        return stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
        os.unlink(probe_path)


def write(path: str, frontmatter: dict[str, Any], body: str) -> None:
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    target_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(target_dir, exist_ok=True)

    # NamedTemporaryFile creates its file at 0600 on POSIX regardless of the
    # process umask, and os.replace carries that mode straight through to
    # `path` -- so every rewrite of an existing lesson/trace silently
    # tightened its permissions to owner-only, locking out anyone else in a
    # shared team repo or CI checkout who could read/write it a moment ago.
    # Fixed by restoring the mode the destination already had (a rewrite
    # should not change who can read a file), or -- for a file that does
    # not exist yet -- the mode a plain `open(path, "w")` would have
    # produced under the current umask, so a NEW file's permissions still
    # respect the umask exactly as everyone expects `open()` to.
    try:
        want_mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        want_mode = _new_file_mode(target_dir)

    temp_file = tempfile.NamedTemporaryFile(
        dir=target_dir,
        delete=False,
        mode="w",
        encoding="utf-8",
        newline="\n",
    )
    temp_path = temp_file.name
    try:
        with temp_file as fh:
            fh.write("---\n")
            fh.write(fm_text)
            fh.write("---\n\n")
            fh.write(body.rstrip("\n") + "\n")
            # Without this, the rename that follows can land before the
            # write it points at is actually on disk: a crash or power
            # loss in that window leaves `path` pointing at a zero-byte or
            # truncated inode even though os.replace() itself is atomic --
            # atomicity of the rename says nothing about durability of the
            # data it's renaming.
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp_path, want_mode)
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise


def _lock_exclusive(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    # msvcrt.locking(LK_LOCK) only retries internally for ~10 seconds
    # before raising OSError -- looping on that here is what turns it into
    # an unbounded blocking wait, matching flock(LOCK_EX)'s semantics.
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            return
        except OSError:
            time.sleep(0.05)


def _unlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _acquire_lock_fd(lock_path: str) -> int:
    if fcntl is None:
        # Windows has no equivalent of unlinking a still-open file out from
        # under a concurrent opener, so there is no analogous "did the path
        # get swapped to a new inode while I waited" race to recheck for
        # here -- open, lock, done.
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        _lock_exclusive(fd)
        return fd

    while True:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        _lock_exclusive(fd)
        try:
            fd_stat = os.fstat(fd)
            path_stat = os.stat(lock_path)
            same_inode = (fd_stat.st_dev, fd_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)
        except OSError:
            same_inode = False
        if same_inode:
            return fd
        # Lost a race with a concurrent release that unlinked this path and
        # a third opener that recreated it: this fd is locked, but on an
        # inode `lock_path` no longer names. Let it go and try again
        # against whatever lives at the path now.
        _unlock(fd)
        os.close(fd)


def _release_lock_fd(fd: int, lock_path: str) -> None:
    """Release the lock and best-effort clean up `lock_path` so it does not
    accumulate one orphaned file per ever-locked path forever.

    POSIX: unlink while STILL HOLDING the lock, and only if `lock_path`
    still names the inode this fd has open. Unlinking after unlocking (or
    unconditionally) would race a concurrent opener that already holds a
    blocked flock() on this same inode: this process's unlink swaps in
    nothing, a still-later opener creates a THIRD, different inode and
    locks it uncontended, and now two callers are inside the critical
    section at once on two different inodes -- exactly the double-lock
    hazard this module's write() design already guards against for `path`
    itself. Checking device+inode match right before unlinking (while
    still exclusive) and pairing it with the recheck loop in
    _acquire_lock_fd is what keeps that from happening here too.

    Windows: the reverse order. A file with any open handle generally
    cannot be unlinked at all, so cleanup has to happen after this
    process's own handle is closed; if another process still has it open
    at that point the unlink simply (and safely) fails.
    """
    if fcntl is not None:
        try:
            fd_stat = os.fstat(fd)
            path_stat = os.stat(lock_path)
            if (fd_stat.st_dev, fd_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino):
                os.unlink(lock_path)
        except OSError:
            pass
        _unlock(fd)
        os.close(fd)
    else:
        _unlock(fd)
        os.close(fd)
        try:
            os.unlink(lock_path)
        except OSError:
            pass


@contextlib.contextmanager
def locked(path: str):
    """Serialize a read-modify-write critical section against `path`
    across processes: `with frontmatter.locked(path): fm, body =
    frontmatter.read(path); ...; frontmatter.write(path, fm, body)`.

    write()'s NamedTemporaryFile + os.replace() makes one call to write()
    atomic -- readers never see a half-written file -- but atomicity of a
    single write is not the same as safety for two concurrent
    read-modify-writers. If writer A and writer B each independently read
    the file, change a DIFFERENT field, and write back, both writes are
    individually atomic and neither corrupts the file -- but B's
    os.replace() completes after A's, so B's write silently overwrites
    A's change with the pre-A content B started from. No error, no
    exception, no corrupted file -- just a logical update quietly lost.
    Reproduced directly: two OS processes racing an approve-style
    read-modify-write on the same lesson file lost one of the two changes
    in 5/5 runs.

    Locks a dedicated, stable sibling file (`path + ".lock"`), NOT `path`
    itself. Locking `path` directly would defeat itself against write()'s
    own atomic-replace design: os.replace() swaps in a new inode, so a
    second writer that opened `path` (and is blocked waiting for the
    lock) is blocked on the OLD inode: once unblocked it reads through
    its already-open file descriptor, seeing the pre-replace content, not
    what the first writer just wrote. A lock file that no writer ever
    replaces keeps the same inode across every acquisition, so "holds the
    lock" and "sees the latest content" never drift apart -- this is
    exactly the "synchronize on a shared lock identity, not independent
    temporary files" fix, as opposed to each writer locking its own
    NamedTemporaryFile (which never contends with anyone).

    Uses fcntl.flock on POSIX and msvcrt.locking on Windows (native, not
    WSL -- WSL is a real Linux kernel and gets fcntl like any other POSIX
    platform). On a platform with neither module this degrades to no
    synchronization at all rather than raising -- consistent with this
    module's existing policy that a robustness feature must never be the
    reason a capture/approve/write fails outright -- but that means the
    race this function exists to close is NOT closed there. This is a
    known, real limitation, not a claim of full cross-platform
    correctness.

    Does not address: a process crashing while holding the lock (the OS
    releases the lock automatically when the holding process's file
    descriptors close, including on crash, so a stale lock cannot outlive
    its process); NFS or other network filesystems, where these locking
    primitives are unreliable or unsupported -- this is designed for the
    local filesystem `memory/` is expected to live on.
    """
    if fcntl is None and msvcrt is None:
        yield
        return

    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    fd = _acquire_lock_fd(lock_path)
    try:
        yield
    finally:
        _release_lock_fd(fd, lock_path)
