"""Read/write Markdown files with YAML frontmatter (the CommonTrace file format)."""
from __future__ import annotations

import collections
import contextlib
import copy
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


def _parse(fm_text: str) -> Any:
    cached = _PARSED.get(fm_text)
    if cached is None:
        cached = load_text(fm_text)
        _PARSED[fm_text] = cached
        while len(_PARSED) > _PARSED_MAX:
            _PARSED.popitem(last=False)
    else:
        _PARSED.move_to_end(fm_text)
    return copy.deepcopy(cached)


def read(path: str) -> tuple[dict[str, Any], str]:
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
        fm = _parse(fm_text)
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
    """Validate an ``expires`` frontmatter value, returning normalized ISO 8601.

    Accepts ``YYYY-MM-DD`` or full ISO 8601 (the same inputs
    ``lesson_cache.parse_moment`` accepts). Raises ``FrontmatterError`` on
    empty or unparseable input.
    """
    from commontrace import ttl

    try:
        return ttl.parse_expiry(value).isoformat()  # type: ignore[arg-type]
    except ValueError as exc:
        raise FrontmatterError(f"invalid lesson `expires` value {value!r}: {exc}") from exc


_DIR_MODE_CACHE: dict[str, int] = {}


def _new_file_mode(target_dir: str) -> int:
    resolved_dir = os.path.abspath(target_dir)
    cached = _DIR_MODE_CACHE.get(resolved_dir)
    if cached is not None:
        return cached

    probe_path = os.path.join(target_dir, f".commontrace-umask-probe-{uuid.uuid4().hex}")
    fd = os.open(probe_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        _DIR_MODE_CACHE[resolved_dir] = mode
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


def _lock_exclusive(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
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
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        _lock_exclusive(fd)
        return fd

    while True:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        _lock_exclusive(fd)
        try:
            fd_stat = os.fstat(fd)
            path_stat = os.stat(lock_path)
            same_inode = (fd_stat.st_dev, fd_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)
        except OSError:
            same_inode = False
        if same_inode:
            return fd
        _unlock(fd)
        os.close(fd)


def _release_lock_fd(fd: int, lock_path: str) -> None:
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
    fd = _acquire_lock_fd(lock_path)
    try:
        yield
    finally:
        _release_lock_fd(fd, lock_path)
