"""The assignment log, exported so somebody else can check the arithmetic.

WHY THIS EXISTS
---------------
Every number this product bills on is computed by this product. The value
ledger (commontrace/value.py) makes the invoice tamper-evident and, with a
signing key, authenticates who issued it -- but both of those establish that
the ISSUER'S OWN ARITHMETIC was not altered after the fact. Neither lets the
customer check the arithmetic itself. A finance function asked to accept
"our system says you owe us this" has no way to disagree except in principle.

The fix is the raw data: one row per (memory, occasion) arm decision, with
the outcome, in a format anyone can load into R, a notebook, or a
spreadsheet and re-run the comparison from. That is the whole artifact the
audit asks for -- an "independently anchored raw-data export" -- and the
anchoring is the second half: the export carries a digest, and that digest
is what the signed ledger commits to, so the numbers on the invoice and the
rows they were computed from are provably the same set.

WHAT MAKES IT CHECKABLE RATHER THAN JUST AVAILABLE
--------------------------------------------------
Three properties, and the third is the one that is easy to get wrong:

1. **One row per arm decision, including the ones the estimate drops.** An
   occasion that was assigned an arm and never reported IS the attrition
   question; exporting only resolved rows would hand over a record with the
   evidence already removed -- the same mistake hub/crud.py's query had to
   stop making.

2. **The revision each memory was on.** A treatment that changed mid-run is
   two treatments, and an export that says only "lesson_x" cannot show that.

3. **A digest over the CONTENT, order-independent.** Rows come back from a
   database in whatever order the planner chose; a digest over the file as
   written would differ between two exports of identical data and would
   prove nothing. Hashing sorted canonical rows means the digest identifies
   the DATA SET, which is what the ledger needs to commit to.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass

#: Column order, fixed. An export whose columns move is an export whose
#: consumer's script breaks silently on the next invoice.
COLUMNS = (
    "memory",
    "occasion_id",
    "arm",            # "injected" | "withheld" -- spelled out, not a bare bool
    "succeeded",      # "true" | "false" | "" (no outcome reported)
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
    """A digest that identifies the DATA, not the file.

    Rows are canonicalized and sorted before hashing, so two exports of the
    same assignments agree regardless of the order a database returned them
    in -- which is the only way this can be the thing a ledger commits to.
    """
    rows = sorted("\x1f".join(_row(a)) for a in assignments)
    payload = _DIGEST_DOMAIN + "\x1e" + "\x1e".join(rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def export(assignments) -> RawExport:
    """Every arm decision, as CSV, plus the digest over the same rows.

    Sorted by (memory, occasion) rather than left in query order: an export a
    customer diffs against last month's should differ only where the data
    did.
    """
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
    """Recompute the digest from an exported CSV and compare.

    Deliberately reimplementable: the rule is "canonical rows, sorted,
    joined, SHA-256", and it is written out in `digest_of` above so a
    customer's auditor can do it in whatever language they audit in and get
    the same answer from the same file.
    """
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
