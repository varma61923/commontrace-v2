"""The CommonTrace Knowledge Base: signature helpers and input validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from commontrace import overlap

COMMONS_NUM_PERM = overlap.DEFAULT_NUM_PERM

DEFAULT_COMMONS_THRESHOLD = overlap.DEFAULT_MATCH_THRESHOLD

MAX_SUBMITTED_FAILURES = 500
MAX_LABEL_CHARS = 200

MAX_SEARCH_CANDIDATES = 25
DEFAULT_SEARCH_CANDIDATES = 5

MAX_HITS_PER_TRACE_PER_QUERY = 20

MAX_COMMONS_CORPUS = 20_000

try:  # pragma: no cover - exercised by whichever path the environment has
    import numpy as _np
except ImportError:
    _np = None

MAX_COMMONS_CORPUS_NO_NUMPY = 2_000


def max_corpus_scan() -> int:
    """The per-query corpus ceiling for the matcher this host will use."""
    return MAX_COMMONS_CORPUS if _np is not None else MAX_COMMONS_CORPUS_NO_NUMPY


STANDING_DISPUTED = "disputed"
STANDING_STALE = "stale"
STANDING_ESTABLISHED = "established"
STANDING_UNPROVEN = "unproven"

VALID_STANDINGS = (
    STANDING_DISPUTED,
    STANDING_STALE,
    STANDING_ESTABLISHED,
    STANDING_UNPROVEN,
)

MIN_VOTES_FOR_STANDING = 3

COMMONS_VOTER_MIN_TRACES = 5
COMMONS_VOTER_MIN_AGE_HOURS = 24


def org_is_established(
    *,
    trace_count: int,
    org_created_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    if trace_count < COMMONS_VOTER_MIN_TRACES:
        return False
    if org_created_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    if org_created_at.tzinfo is None:
        org_created_at = org_created_at.replace(tzinfo=timezone.utc)
    return (now - org_created_at) >= timedelta(hours=COMMONS_VOTER_MIN_AGE_HOURS)


def vote_counts_toward_standing(
    *,
    trace_count: int,
    org_created_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    return org_is_established(
        trace_count=trace_count, org_created_at=org_created_at, now=now
    )


def hit_counts_toward_quality_signal(
    *,
    trace_count: int,
    org_created_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    return org_is_established(
        trace_count=trace_count, org_created_at=org_created_at, now=now
    )

DISPUTED_TRUST_CEILING = 0.5

ESTABLISHED_TRUST_FLOOR = 0.75


def entry_standing(
    *,
    trust: float,
    votes: int,
    review_after: datetime | None,
    now: datetime | None = None,
) -> str:
    """One label for how much weight a Knowledge Base entry currently earns."""
    if votes >= MIN_VOTES_FOR_STANDING and trust < DISPUTED_TRUST_CEILING:
        return STANDING_DISPUTED
    if review_after is not None:
        now = now or datetime.now(timezone.utc)
        if review_after.tzinfo is None:
            review_after = review_after.replace(tzinfo=timezone.utc)
        if review_after <= now:
            return STANDING_STALE
    if votes >= MIN_VOTES_FOR_STANDING and trust >= ESTABLISHED_TRUST_FLOOR:
        return STANDING_ESTABLISHED
    return STANDING_UNPROVEN


def counts_as_coverage(standing: str) -> bool:
    return standing != STANDING_DISPUTED


class CommonsInputError(ValueError):
    ...


def matchable_text(title: str, context_text: str, tags: list[str] | None) -> str:
    """The text a commons trace is signed on."""
    return " ".join([title or "", context_text or "", " ".join(tags or [])])


def signature_for(title: str, context_text: str, tags: list[str] | None) -> list[int]:
    """MinHash signature for a trace entering the commons."""
    return overlap.minhash(matchable_text(title, context_text, tags), COMMONS_NUM_PERM)


def validate_submitted_failures(failures: object) -> list[tuple[str, list[int]]]:
    """Coerce and bound a client-submitted failure list."""
    if not isinstance(failures, (list, tuple)):
        raise CommonsInputError("failures must be a list")
    if len(failures) > MAX_SUBMITTED_FAILURES:
        raise CommonsInputError(
            f"too many failures submitted ({len(failures)}); "
            f"the maximum is {MAX_SUBMITTED_FAILURES} per request"
        )

    out: list[tuple[str, list[int]]] = []
    for i, item in enumerate(failures):
        if not isinstance(item, dict):
            raise CommonsInputError(f"failures[{i}] must be an object with 'label' and 'signature'")
        raw_sig = item.get("signature")
        if not isinstance(raw_sig, (list, tuple)):
            raise CommonsInputError(f"failures[{i}].signature must be a list of integers")
        if len(raw_sig) != COMMONS_NUM_PERM:
            raise CommonsInputError(
                f"failures[{i}].signature has {len(raw_sig)} values; "
                f"this Hub's commons uses num_perm={COMMONS_NUM_PERM}. "
                "Re-sign with a matching width."
            )
        sig = _coerce_signature(raw_sig, f"failures[{i}].signature")
        label = str(item.get("label") or f"failure-{i}")[:MAX_LABEL_CHARS]
        out.append((label, sig))
    return out


def _coerce_signature(raw_sig: object, field: str) -> list[int]:
    if not isinstance(raw_sig, (list, tuple)):
        raise CommonsInputError(f"{field} must be a list of integers")
    if len(raw_sig) != COMMONS_NUM_PERM:
        raise CommonsInputError(
            f"{field} has {len(raw_sig)} values; this Hub's commons uses "
            f"num_perm={COMMONS_NUM_PERM}. Re-sign with a matching width."
        )
    sig: list[int] = []
    for v in raw_sig:
        if not isinstance(v, int) or isinstance(v, bool) or not (0 <= v <= 2**64 - 1):
            raise CommonsInputError(f"{field} must contain only integers in [0, 2**64 - 1]")
        sig.append(v)
    return sig


def validate_query_signature(signature: object) -> list[int]:
    return _coerce_signature(signature, "query_signature")


def estimate(sig_a: list[int], sig_b: list[int]) -> float:
    """Estimated Jaccard similarity between two signatures of equal width."""
    return overlap.estimate_jaccard(sig_a, sig_b)


def best_matches(
    submitted: list[tuple[str, list[int]]],
    corpus_signatures: list[list[int]],
) -> list[tuple[int, float]]:
    if len(corpus_signatures) == 0:
        return [(-1, 0.0) for _ in submitted]

    if _np is not None:
        corpus = _np.asarray(corpus_signatures, dtype=_np.uint64)
        out: list[tuple[int, float]] = []
        for _label, sig in submitted:
            sims = (corpus == _np.array(sig, dtype=_np.uint64)).sum(axis=1) / COMMONS_NUM_PERM
            idx = int(sims.argmax())
            sim = float(sims[idx])
            out.append((idx, sim) if sim > 0.0 else (-1, 0.0))
        return out

    out = []
    for _label, sig in submitted:
        best_idx, best_sim = -1, 0.0
        for i, candidate in enumerate(corpus_signatures):
            sim = estimate(sig, candidate)
            if sim > best_sim:
                best_idx, best_sim = i, sim
        out.append((best_idx, best_sim))
    return out


def rank_candidates(
    query_signature: list[int],
    corpus_signatures: list[list[int]],
    top_k: int,
) -> list[tuple[int, float]]:
    if len(corpus_signatures) == 0 or top_k <= 0:
        return []

    if _np is not None:
        corpus = _np.asarray(corpus_signatures, dtype=_np.uint64)
        sims = (corpus == _np.array(query_signature, dtype=_np.uint64)).sum(axis=1) / COMMONS_NUM_PERM
        order = _np.argsort(-sims, kind="stable")[:top_k]
        return [(int(i), float(sims[i])) for i in order if float(sims[i]) > 0.0]

    scored = [(i, estimate(query_signature, c)) for i, c in enumerate(corpus_signatures)]
    scored = [(i, s) for i, s in scored if s > 0.0]
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]
