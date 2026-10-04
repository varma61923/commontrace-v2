"""Evidence-grounded observations consolidated from reinforced atomic facts.

An observation is a distilled, citable claim: the statement, the evidence
quotes with their source ids, a proof_count, and a density-based trend.

Hindsight TEMPR practice (cited, not vendored): claims earn trust through
counted, sourced proof rather than a single assertion -- ``proof_count``
records how many independent confirmations the underlying fact gathered,
and ``observation_boost`` turns that count into a bounded retrieval bonus
(``min(0.3, 0.05 * n)``) so often-proven observations rank above thin ones
without swamping lexical relevance.

Trend is derived from evidence density over trailing windows: 30d vs the
prior 60d (up to 90d). "new" when all evidence is recent, "stale" when the
newest evidence is older than 90d, "strengthening"/"weakening"/"stable" by
how the recent 30d count compares with the prior 60d count.

Storage: memory/observations/observations.jsonl, atomic tmp+rename writes
via commontrace._jsonl, matching the rest of the store. Stdlib only.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from commontrace import _jsonl, frontmatter, hierarchical, paths

TRENDS = ("stable", "strengthening", "weakening", "new", "stale")
BOOST_CAP = 0.3
BOOST_STEP = 0.05

_TRACE_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def observation_boost(proof_count: int | float | str) -> float:
    """Bounded retrieval bonus: ``min(0.3, 0.05 * proof_count)``; 0 for junk."""
    try:
        n = int(proof_count)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return round(min(BOOST_CAP, BOOST_STEP * max(0, n)), 4)


@dataclass
class Observation:
    id: str
    statement: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    proof_count: int = 0
    trend: str = "new"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Observation":
        clean = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        clean["evidence"] = [e for e in (clean.get("evidence") or []) if isinstance(e, dict)]
        clean["proof_count"] = int(clean.get("proof_count") or 0)
        if clean.get("trend") not in TRENDS:
            clean["trend"] = "new"
        return cls(**clean)


def _observations_file(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "observations", "observations.jsonl")


def load_observations(root: str) -> dict[str, Observation]:
    """Every observation on disk, keyed by id; unreadable rows are skipped."""
    out: dict[str, Observation] = {}
    for row in _jsonl.read_rows(_observations_file(root)):
        try:
            observation = Observation.from_dict(row)
        except (TypeError, ValueError):
            continue
        if observation.id:
            out[observation.id] = observation
    return out


def save_observations(root: str, observations: dict[str, Observation]) -> None:
    """Atomically replace the JSONL store, sorted by id for a stable diff."""
    path = _observations_file(root)
    with _jsonl.locked(path):
        _jsonl.write_rows(path, (observations[key].to_dict() for key in sorted(observations)))


def get_observation(root: str, observation_id: str) -> Observation | None:
    return load_observations(root).get(str(observation_id))


def _trace_dates(root: str) -> dict[str, str]:
    """Trace id -> date (from the ``YYYY-MM-DD_`` filename prefix), for evidence timing."""
    out: dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(paths.traces_dir(root), "*.md"))):
        try:
            fm, _body = frontmatter.read(path)
        except Exception:  # noqa: BLE001 - unreadable traces are skipped
            continue
        trace_id = str(fm.get("id", "") or "")
        match = _TRACE_DATE_RE.match(os.path.basename(path))
        if trace_id and match:
            out[trace_id] = match.group(1)
    return out


def _trend(evidence_times: list[str], *, now: datetime, created_at: str = "") -> str:
    """stable/strengthening/weakening/new/stale from 30d/90d evidence density."""
    stamps = sorted(stamp for stamp in (_parse(t) for t in evidence_times) if stamp is not None)
    if not stamps:
        created = _parse(created_at)
        if created is not None and created < now - timedelta(days=90):
            return "stale"
        return "new"
    if stamps[-1] < now - timedelta(days=90):
        return "stale"
    if stamps[0] >= now - timedelta(days=30):
        return "new"
    recent = sum(1 for stamp in stamps if stamp >= now - timedelta(days=30))
    middle = sum(1 for stamp in stamps if now - timedelta(days=90) <= stamp < now - timedelta(days=30))
    if recent > middle:
        return "strengthening"
    if recent < middle:
        return "weakening"
    return "stable"


def _observation_id(statement: str) -> str:
    norm = re.sub(r"\s+", " ", statement.strip().lower())
    return "obs-" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def consolidate_facts(root: str, now: str | None = None) -> list[Observation]:
    """Group reinforced facts (confirmations >= 2) into observations.

    Deterministic for the same store content and the same ``now``: ids derive
    from the statement, evidence is sorted by source id, and every timestamp
    is either evidence-derived or the passed ``now``.
    """
    now_dt = _parse(now) or datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    facts = hierarchical.load_facts(root)
    trace_dates = _trace_dates(root)
    existing = load_observations(root)
    out: dict[str, Observation] = {}
    for fact_id in sorted(facts):
        fact = facts[fact_id]
        if fact.status != "active" or fact.forgotten or fact.confirmations < 2:
            continue
        evidence: list[dict[str, Any]] = []
        for trace_id in sorted({str(t) for t in fact.source_traces if str(t).strip()}):
            evidence.append({
                "quote": fact.statement,
                "source_id": trace_id,
                "at": trace_dates.get(trace_id) or fact.created_at or "",
            })
        if not evidence:
            evidence.append({"quote": fact.statement, "source_id": "", "at": fact.created_at or ""})
        trend = _trend([str(e.get("at", "")) for e in evidence], now=now_dt, created_at=fact.created_at)
        observation_id = _observation_id(fact.statement)
        previous = existing.get(observation_id)
        out[observation_id] = Observation(
            id=observation_id,
            statement=fact.statement,
            evidence=evidence,
            proof_count=fact.confirmations,
            trend=trend,
            created_at=previous.created_at if previous is not None else now_iso,
            updated_at=now_iso,
        )
    save_observations(root, out)
    return [out[key] for key in sorted(out)]
