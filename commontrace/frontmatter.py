"""Read/write Markdown files with YAML frontmatter (the CommonTrace file format)."""
from __future__ import annotations

import collections
import contextlib
import copy
import errno
import math
import os
import re
import stat
import sys
import tempfile
import threading
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

_DELIM_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)


_DROPPED_YAML_TAGS = frozenset({"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"})


class _StrictBoolLoader(yaml.SafeLoader):
    yaml_implicit_resolvers = {
        first_char: [
            (tag, regexp) for tag, regexp in resolvers if tag not in _DROPPED_YAML_TAGS
        ]
        for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def compose_node(self, parent, index):
        event = self.peek_event()
        if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise yaml.composer.ComposerError(
                None, None,
                "YAML anchors/aliases are not permitted in CommonTrace frontmatter",
                event.start_mark,
            )
        return super().compose_node(parent, index)


_StrictBoolLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


if getattr(yaml, "__with_libyaml__", False):
    class _CStrictBoolLoader(yaml.CSafeLoader):  # type: ignore[name-defined, misc]
        yaml_implicit_resolvers = _StrictBoolLoader.yaml_implicit_resolvers
else:  # pragma: no cover -- exercised where PyYAML was built without libyaml
    _CStrictBoolLoader = None


def load_text(fm_text: str) -> Any:
    if _CStrictBoolLoader is not None and "&" not in fm_text and "*" not in fm_text:
        try:
            return yaml.load(fm_text, Loader=_CStrictBoolLoader)  # nosec B506
        except yaml.YAMLError:
            pass
    return yaml.load(fm_text, Loader=_StrictBoolLoader)  # nosec B506


class FrontmatterError(ValueError):
    ...


_PARSED: "collections.OrderedDict[str, Any]" = collections.OrderedDict()
_PARSED_MAX = 4096
_PARSED_MAX_BYTES = 16 * 1024 * 1024
_PARSED_BYTES = 0
_PARSED_SIZES: dict[str, int] = {}
_PARSED_LOCK = threading.Lock()
_MISSING = object()


def _reset_parsed_after_fork() -> None:
    global _PARSED_LOCK, _PARSED_BYTES
    _PARSED_LOCK = threading.Lock()
    # The parent may have forked while another thread was updating accounting.
    # Start with an empty memo rather than inheriting a partially updated LRU.
    _PARSED.clear()
    _PARSED_SIZES.clear()
    _PARSED_BYTES = 0


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_parsed_after_fork)


def _retained_size(value: Any, seen: set[int]) -> int:
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_retained_size(k, seen) + _retained_size(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple, set, frozenset)):
        size += sum(_retained_size(item, seen) for item in value)
    return size


def _parse(fm_text: str) -> Any:
    global _PARSED_BYTES
    with _PARSED_LOCK:
        # Private callers may clear the memo (for example between test cases).
        if not _PARSED:
            _PARSED_SIZES.clear()
            _PARSED_BYTES = 0
        cached = _PARSED.get(fm_text, _MISSING)
        if cached is not _MISSING:
            _PARSED.move_to_end(fm_text)
    if cached is _MISSING:
        # Parsing and object traversal run without serializing unrelated reads.
        cached = load_text(fm_text)
        # Include a conservative allowance for OrderedDict bookkeeping, the
        # auxiliary size mapping, and its integer entry alongside YAML objects.
        size = _retained_size((fm_text, cached), set()) + 256
        if size <= _PARSED_MAX_BYTES:
            with _PARSED_LOCK:
                if fm_text not in _PARSED:
                    _PARSED[fm_text] = cached
                    _PARSED_SIZES[fm_text] = size
                    _PARSED_BYTES += size
                _PARSED.move_to_end(fm_text)
                while len(_PARSED) > _PARSED_MAX or _PARSED_BYTES > _PARSED_MAX_BYTES:
                    key, _ = _PARSED.popitem(last=False)
                    _PARSED_BYTES -= _PARSED_SIZES.pop(key)
    return copy.deepcopy(cached)


def read(path: str) -> tuple[dict[str, Any], str]:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise FrontmatterError(f"cannot read {path}: {exc}") from exc
    if not content.startswith("---"):
        return {}, content
    first = _DELIM_RE.search(content)
    second = _DELIM_RE.search(content, first.end()) if first else None
    if second is None:
        return {}, content
    fm_text = content[first.end():second.start()]
    try:
        fm = _parse(fm_text)
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"{path}: malformed YAML frontmatter: {exc}") from exc
    if fm is None:
        fm = {}
    if not isinstance(fm, dict):
        raise FrontmatterError(
            f"{path}: frontmatter must be a YAML mapping, got {type(fm).__name__}"
        )
    body = content[second.end():].lstrip("\n")
    return fm, body


def read_body(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise FrontmatterError(f"cannot read {path}: {exc}") from exc
    if not content.startswith("---"):
        return content
    first = _DELIM_RE.search(content)
    second = _DELIM_RE.search(content, first.end()) if first else None
    if second is None:
        return content
    return content[second.end():].lstrip("\n")


def validate_expires(value: object) -> str:
    """Validate an ``expires`` frontmatter value, returning normalized ISO 8601."""
    from commontrace import ttl

    try:
        return ttl.parse_expiry(value).isoformat()  # type: ignore[arg-type]
    except ValueError as exc:
        raise FrontmatterError(f"invalid lesson `expires` value {value!r}: {exc}") from exc


def _new_file_mode(target_dir: str) -> int:
    # A directory does not fix process umask or inherited ACLs. Probe each new
    # file without temporarily changing the process-wide umask in other threads.
    probe_path = os.path.join(target_dir, f".commontrace-umask-probe-{uuid.uuid4().hex}")
    fd = os.open(probe_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        return mode
    finally:
        os.close(fd)
        os.unlink(probe_path)


def write(path: str, frontmatter: dict[str, Any], body: str) -> None:
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    target_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(target_dir, exist_ok=True)

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


_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_RETRY_SECONDS = 0.01


def _lock_exclusive(fd: int, deadline: float) -> None:
    while True:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            retryable = {errno.EACCES, errno.EAGAIN, errno.EINTR}
            if fcntl is None:
                retryable.add(errno.EDEADLK)
            if exc.errno not in retryable:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for a frontmatter lock") from exc
            time.sleep(min(_LOCK_RETRY_SECONDS, remaining))


def _unlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _acquire_lock_fd(lock_path: str, timeout: float = _LOCK_TIMEOUT_SECONDS) -> int:
    deadline = time.monotonic() + timeout
    while True:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _lock_exclusive(fd, deadline)
            if fcntl is None:
                return fd
            try:
                fd_stat = os.fstat(fd)
                path_stat = os.stat(lock_path)
                same_inode = (fd_stat.st_dev, fd_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)
            except OSError:
                same_inode = False
            if same_inode:
                return fd
            _unlock(fd)
        except BaseException as exc:
            os.close(fd)
            if isinstance(exc, TimeoutError):
                raise TimeoutError(f"timed out acquiring frontmatter lock {lock_path}") from exc
            raise
        os.close(fd)
        # A replaced/unlinked lock inode must share the original acquisition
        # deadline; resetting it here would still permit an unbounded wait.
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out acquiring frontmatter lock {lock_path}")


def _release_lock_fd(fd: int, lock_path: str) -> None:
    if fcntl is not None:
        try:
            fd_stat = os.fstat(fd)
            path_stat = os.stat(lock_path)
            if (fd_stat.st_dev, fd_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino):
                os.unlink(lock_path)
        except OSError:
            pass
        try:
            _unlock(fd)
        finally:
            os.close(fd)
    else:
        try:
            _unlock(fd)
        finally:
            os.close(fd)
        try:
            os.unlink(lock_path)
        except OSError:
            pass


@contextlib.contextmanager
def locked(path: str, *, timeout: float = _LOCK_TIMEOUT_SECONDS):
    """Serialize file updates, failing after ``timeout`` seconds of contention.

    ``timeout=0`` tries once without waiting. Time spent inside the protected
    block is not limited. Acquisition errors always close the lock descriptor.
    """
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("frontmatter lock timeout must be finite and non-negative")
    if fcntl is None and msvcrt is None:
        import warnings

        warnings.warn(
            "frontmatter.locked: no fcntl/msvcrt on this platform; "
            "the lock is a no-op and concurrent read-modify-write is unsafe",
            RuntimeWarning, stacklevel=2,
        )
        yield
        return

    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    fd = _acquire_lock_fd(lock_path, timeout)
    try:
        yield
    finally:
        _release_lock_fd(fd, lock_path)
