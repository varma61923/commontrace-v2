"""The assignment log, exported so somebody else can check the arithmetic."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass

COLUMNS = (
    "memory",
    "occasion_id",
    "arm",
    "succeeded",
    "assigned_at",
    "resolved_at",
    "revision",
    "holdout_rate",
    "salt",
)

_DIGEST_DOMAIN = "commontrace-raw-assignments-v1"


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row(assignment) -> tuple[str, ...]:
    return (
        _cell(getattr(assignment, "lesson", "")),
        _cell(getattr(assignment, "occasion_id", "")),
        "injected" if getattr(assignment, "injected", False) else "withheld",
        _cell(getattr(assignment, "succeeded", None)),
        _cell(getattr(assignment, "at", None)),
        _cell(getattr(assignment, "resolved_at", None)),
        _cell(getattr(assignment, "revision", None)),
        _cell(getattr(assignment, "rate", "")),
        _cell(getattr(assignment, "salt", "")),
    )


@dataclass(frozen=True)
class RawExport:
    csv_text: str
    digest: str
    n_rows: int
    n_occasions: int
    n_memories: int
    n_unresolved: int

    @property
    def summary(self) -> str:
        return (
            f"{self.n_rows:,} arm decisions across {self.n_occasions:,} occasions and "
            f"{self.n_memories:,} memories ({self.n_unresolved:,} with no outcome "
            f"reported), sha256 {self.digest[:16]}..."
        )


def digest_of(assignments) -> str:
    """A digest that identifies the DATA, not the file."""
    rows = sorted("\x1f".join(_row(a)) for a in assignments)
    payload = _DIGEST_DOMAIN + "\x1e" + "\x1e".join(rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def export(assignments) -> RawExport:
    """Every arm decision, as CSV, plus the digest over the same rows."""
    rows = sorted(_row(a) for a in assignments)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(rows)

    occasions = {row[1] for row in rows}
    memories = {row[0] for row in rows}
    unresolved = sum(1 for row in rows if row[3] == "")
    return RawExport(
        csv_text=buffer.getvalue(),
        digest=digest_of(assignments),
        n_rows=len(rows),
        n_occasions=len(occasions),
        n_memories=len(memories),
        n_unresolved=unresolved,
    )


def verify(csv_text: str, digest: str) -> bool:
    """Recompute the digest from an exported CSV and compare."""
    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = next(reader)
    except StopIteration:
        return False
    if tuple(header) != COLUMNS:
        return False
    rows = sorted("\x1f".join(row) for row in reader if row)
    payload = _DIGEST_DOMAIN + "\x1e" + "\x1e".join(rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest() == digest
