"""Private, replaceable exact vector snapshots for corpora exceeding the RAM cache.

SQLite remains the source of truth. A single immutable NumPy file per space and
model allows bounded sequential scans without decoding the interaction log or
vector blobs on every request. Publication never regresses a source generation.
"""
from __future__ import annotations

import contextlib
import os
import tempfile

FORMAT = 1
MAX_FILES = 16
MAX_BYTES = 4 * 1024 * 1024 * 1024


def path_for(store, tag):
    from commontrace.conversation.store import _hash

    identity = f"{FORMAT}:{store.path}:{store._units_identity}:{tag}"
    return os.path.join(os.path.dirname(store.path), f"dense-{_hash(identity)}.npy")


def dtype(np, dimensions):
    return np.dtype([("id", "<i8"), ("turn", "<i8"), ("hash", "S32"),
                     ("vector", "<f2", (dimensions,))])


def load(store, tag, np, revision, dimensions, *, check_ids=True):
    """Validate a snapshot's source and shape before returning its read-only map.

    A caller disabling the full id-order check must validate every selected
    id/turn/hash against the authoritative source and must not share that map as
    a globally validated index. This avoids reading unrelated vector pages for
    a small restricted scope without weakening proof of selected evidence.
    """
    if not store._units_identity:
        return None
    try:
        records = np.load(path_for(store, tag), mmap_mode="r", allow_pickle=False)
        if not isinstance(records, np.memmap):
            if hasattr(records, "close"):
                records.close()
            return None
        if records.ndim != 1 or not len(records) or records.dtype != dtype(np, dimensions):
            return None
        header = records[0]
        if int(header["id"]) != int(revision) or int(header["turn"]) != FORMAT \
                or header["hash"].decode("ascii") != store._units_identity:
            return None
        if len(records) - 1 != store.db.execute("SELECT COUNT(*) FROM units").fetchone()[0]:
            return None
        if check_ids:
            ids = records["id"][1:]
            if len(ids) and (ids[0] <= 0 or (ids[1:] <= ids[:-1]).any()):
                return None
        # A corrupted/truncated or incorrectly shaped file is an expendable
        # cache miss, never a reason to fail source-backed memory recall.
        return records
    except (OSError, ValueError, EOFError, UnicodeError):
        return None


class Build:
    """Temporary mapped writer; the caller must close it even after inference fails."""

    def __init__(self, store, tag, np, revision, count, dimensions):
        self.store, self.tag, self.np = store, tag, np
        self.revision, self.temporary, self.records = int(revision), None, None
        required = (count + 1) * dtype(np, dimensions).itemsize + 4096
        if store.read_only or not store._units_identity or required > MAX_BYTES or MAX_FILES < 1:
            return
        try:
            import fcntl  # noqa: F401 - portable hosts without file locking stream instead

            fd, self.temporary = tempfile.mkstemp(prefix=f".dense-{os.getpid()}-", suffix=".npy",
                                                  dir=os.path.dirname(store.path))
            os.close(fd)
            self.records = np.lib.format.open_memmap(self.temporary, mode="w+", dtype=dtype(np, dimensions),
                                                     shape=(count + 1,))
            if hasattr(os, "posix_fallocate"):
                # Reserve disk blocks before dirtying mapped pages. A full disk
                # is a normal cache miss, not a SIGBUS while scoring memory.
                with open(self.temporary, "r+b") as backing:
                    os.posix_fallocate(backing.fileno(), 0, os.fstat(backing.fileno()).st_size)
            self.records[0]["id"] = self.revision
            self.records[0]["turn"] = FORMAT
            self.records[0]["hash"] = store._units_identity.encode("ascii")
        except (ImportError, OSError, ValueError):
            self.close()

    def write(self, offset, batch, vectors):
        if self.records is None:
            return
        end = offset + len(batch)
        try:
            page = self.records[offset + 1:end + 1]
            page["id"] = [u for u, _t, _b, _h in batch]
            page["turn"] = [t for _u, t, _b, _h in batch]
            page["hash"] = [h.encode("ascii") for _u, _t, _b, h in batch]
            page["vector"] = vectors
        except OSError:
            self.close()

    def publish(self):
        if self.records is None:
            return None
        import fcntl

        final = path_for(self.store, self.tag)
        try:
            self.records.flush()
            # Serialize only publication across processes; local inference and
            # bulk file writes never hold the lock or a SQLite writer lock.
            # One publication lock per cache directory also coordinates pruning
            # across spaces; lock files cannot grow with model/space churn.
            lock_path = os.path.join(os.path.dirname(final), ".dense.lock")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    existing = self.np.load(final, mmap_mode="r", allow_pickle=False)
                except (OSError, ValueError, EOFError):
                    existing = None
                if isinstance(existing, self.np.memmap) and existing.ndim == 1 \
                        and existing.dtype == self.records.dtype \
                        and len(existing) and int(existing[0]["turn"]) == FORMAT \
                        and existing[0]["hash"] == self.store._units_identity.encode("ascii") \
                        and int(existing[0]["id"]) > self.revision:
                    return None
                os.replace(self.temporary, final)
                self.temporary = None
                _prune(os.path.dirname(final), final)
                # Open the published inode while its publication lock is held.
                # Another process may replace this path after lock release.
                return self.np.load(final, mmap_mode="r", allow_pickle=False)
        except (OSError, ValueError, EOFError):
            return None
        finally:
            self.close()

    def close(self):
        # Releasing this writer leaves any independently opened reader valid.
        self.records = None
        if self.temporary:
            with contextlib.suppress(OSError):
                os.unlink(self.temporary)
            self.temporary = None


def _prune(directory, keep):
    files = []
    for entry in os.scandir(directory):
        if entry.name.startswith(".dense-") and entry.name.endswith(".npy"):
            try:
                pid = int(entry.name.split("-")[1])
                os.kill(pid, 0)
            except ProcessLookupError:
                with contextlib.suppress(OSError):
                    os.unlink(entry.path)
            except (ValueError, PermissionError):
                pass
        if entry.name.startswith("dense-") and entry.name.endswith(".npy") and entry.is_file(follow_symlinks=False):
            stat = entry.stat(follow_symlinks=False)
            files.append((stat.st_mtime_ns, entry.path, stat.st_size))
    total = sum(size for _at, _path, size in files)
    count = len(files)
    for _at, path, size in sorted(files):
        if count <= MAX_FILES and total <= MAX_BYTES:
            break
        if path != keep:
            try:
                os.unlink(path)
            except OSError:
                continue
            count -= 1
            total -= size
