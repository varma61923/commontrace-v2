"""One store's retrieval settings, read by EVERY retriever."""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass

from commontrace import dosage, harm, paths, retrieval

CONFIG_NAME = "retrieval.json"

FUSION_NONE = "none"
FUSION_RRF = "rrf"
FUSION_GATED = "gated"
FUSIONS = (FUSION_NONE, FUSION_RRF, FUSION_GATED)

SEMANTIC_ONLY = "semantic"
SEMANTIC_ONLY_DOSED = "semantic-dosed"

RERANK_NONE = "none"
RERANK_CE = "cross-encoder"
RERANK_CE_FAST = "cross-encoder-fast"
RERANKS = (RERANK_NONE, RERANK_CE, RERANK_CE_FAST)

DEFAULT_RERANK_ENV = "COMMONTRACE_DEFAULT_RERANK"


DEFAULT_FUSION_ENV = "COMMONTRACE_DEFAULT_FUSION"


def default_fusion() -> str:
    override = os.environ.get(DEFAULT_FUSION_ENV, "")
    if override in FUSIONS:
        return override
    from commontrace import semantic_arm

    if default_rerank() != RERANK_NONE and semantic_arm.available():
        return FUSION_GATED
    return FUSION_NONE


def default_rerank() -> str:
    override = os.environ.get(DEFAULT_RERANK_ENV, "")
    if override in RERANKS:
        return override
    from commontrace import rerank_arm

    return RERANK_CE if rerank_arm.available() else RERANK_NONE

_FUSION_LABEL = re.compile(
    r"^(?P<mode>rrf|gated)\((?P<lexical>[^+()@]+)\+semantic(?:@(?P<embedder>[\w.-]+))?\)$")

EMBEDDER_TAGS = {
    "multi-qa-mpnet-base-dot-v1": "",
    "Snowflake/snowflake-arctic-embed-m-v1.5": "arctic-m",
}


def embedder_tag(model_name: str | None) -> str:
    return EMBEDDER_TAGS.get(model_name or "", "")
_RERANK_LABEL = re.compile(r"^ce:(?P<model>[^()]+)\((?P<inner>.+)\)$")


def eligibility_label(
    scorer: str, fusion: str, rerank: str = RERANK_NONE, embedder: str = "",
) -> str:
    if fusion in (FUSION_RRF, FUSION_GATED):
        label = f"{fusion}({scorer}+semantic{'@' + embedder if embedder else ''})"
    else:
        label = scorer
    return rerank_label(label, rerank)


def rerank_label(first_stage: str, rerank: str) -> str:
    """`first_stage`'s label, wrapped with the reranker when one reordered it."""
    if rerank != RERANK_NONE:
        from commontrace import rerank_arm

        return f"ce:{rerank_arm.tag(rerank)}({first_stage})"
    return first_stage


def parse_rerank_label(label: str) -> tuple[str, str]:
    """(first-stage label, rerank mode) for a recorded label."""
    match = _RERANK_LABEL.match(label or "")
    if match:
        from commontrace import rerank_arm

        return match.group("inner"), rerank_arm.mode_for_tag(match.group("model")) or RERANK_CE
    return label, RERANK_NONE


def semantic_only_label(embedder: str = "", *, dosed: bool = False) -> str:
    base = SEMANTIC_ONLY_DOSED if dosed else SEMANTIC_ONLY
    return f"{base}@{embedder}" if embedder else base


def _is_semantic_only(label: str) -> bool:
    return (label or "").partition("@")[0] in (SEMANTIC_ONLY, SEMANTIC_ONLY_DOSED)


def semantic_only_undosed_pinned(root: str) -> bool:
    logged = _last_logged_settings(root)
    if logged is None:
        return False
    return parse_rerank_label(logged[0])[0].partition("@")[0] == SEMANTIC_ONLY


def parse_embedder(label: str) -> str:
    label = parse_rerank_label(label)[0] or ""
    if _is_semantic_only(label):
        return label.partition("@")[2]
    match = _FUSION_LABEL.match(label)
    return (match.group("embedder") or "") if match else ""


def parse_eligibility_label(label: str) -> tuple[str, str]:
    """Inverse of `eligibility_label`: (lexical scorer, fusion mode)."""
    label, _rerank = parse_rerank_label(label)
    match = _FUSION_LABEL.match(label or "")
    if match:
        return match.group("lexical"), match.group("mode")
    if _is_semantic_only(label):
        return retrieval.SCORER_IDF, FUSION_NONE
    return label, FUSION_NONE


def config_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), CONFIG_NAME)


@dataclass(frozen=True)
class RetrievalConfig:
    scorer: str = retrieval.SCORER_IDF
    floor: float = retrieval.DEFAULT_FLOOR
    max_lessons: int = dosage.DEFAULT_MAX_LESSONS
    max_chars: int = dosage.DEFAULT_MAX_CHARS
    redundancy_threshold: float = dosage.DEFAULT_REDUNDANCY_THRESHOLD
    fusion: str = FUSION_NONE
    rrf_k: int = retrieval.DEFAULT_RRF_K
    reliability_weight: float = 0.0
    recency_weight: float = 0.0
    harm_policy: str = harm.POLICY_INFORM
    rerank: str = RERANK_NONE

    @property
    def eligibility(self) -> str:
        """The label an assignment made under these settings records."""
        return eligibility_label(self.scorer, self.fusion, self.rerank)

    def eligibility_label_for(self, *, fused: bool, embedder: str = "") -> str:
        if not fused:
            return eligibility_label(self.scorer, FUSION_NONE)
        return eligibility_label(self.scorer, self.fusion, embedder=embedder)
    configured_at: str = ""
    note: str = ""
    pinned_for_running_experiment: bool = False


def _int_or(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _float_or(value: object, default: float) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def _unit_float_or(value: object, default: float) -> float:
    out = _float_or(value, default)
    return out if 0.0 <= out <= 1.0 else default


def has_recorded_assignments(root: str) -> bool:
    """Whether this store has already logged holdout assignments."""
    from commontrace import holdout_io

    path = holdout_io.holdout_log_path(root)
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _last_logged_settings(root: str) -> tuple[str, float] | None:
    from commontrace import holdout_io

    path = holdout_io.holdout_log_path(root)
    try:
        size = os.path.getsize(path)
        if size == 0:
            return None
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 8192))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed([ln for ln in tail.splitlines() if ln.strip()]):
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        scorer = raw.get("scorer")
        floor = raw.get("floor")
        if scorer and floor is None and _is_semantic_only(parse_rerank_label(str(scorer))[0]):
            return str(scorer), retrieval.DEFAULT_FLOOR
        if scorer and floor is not None:
            try:
                return str(scorer), float(floor)
            except (TypeError, ValueError):
                return None
        return None
    return None


def logged_embedding_model(root: str) -> str | None:
    logged = _last_logged_settings(root)
    if logged is None:
        return None
    if (parse_eligibility_label(logged[0])[1] == FUSION_NONE
            and not _is_semantic_only(parse_rerank_label(logged[0])[0])):
        return None
    tag = parse_embedder(logged[0])
    return next((name for name, t in EMBEDDER_TAGS.items() if t == tag), None)


def _unchosen_fusion(root: str) -> str:
    logged = _last_logged_settings(root)
    if logged is not None:
        return parse_eligibility_label(logged[0])[1]
    if has_recorded_assignments(root):
        return FUSION_NONE
    return default_fusion()


def _unchosen_rerank(root: str) -> str:
    logged = _last_logged_settings(root)
    if logged is not None:
        return parse_rerank_label(logged[0])[1]
    if has_recorded_assignments(root):
        return RERANK_NONE
    return default_rerank()


def read_harm_policy(root: str) -> str:
    try:
        with open(config_path(root), encoding="utf-8") as fh:
            raw = json.load(fh)
        value = raw.get("harm_policy") if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return harm.POLICY_INFORM
    return str(value) if value in harm.POLICIES else harm.POLICY_INFORM


def load_config(root: str) -> RetrievalConfig:
    """This store's retrieval settings, or the right defaults if unset."""
    path = config_path(root)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                scorer = str(raw.get("scorer") or retrieval.SCORER_IDF)
                if scorer not in retrieval.LEXICAL_SCORERS:
                    scorer = retrieval.SCORER_IDF
                return RetrievalConfig(
                    scorer=scorer,
                    floor=_float_or(raw.get("floor"), retrieval.default_floor(scorer)),
                    max_lessons=_int_or(
                        raw.get("max_lessons"), dosage.DEFAULT_MAX_LESSONS),
                    max_chars=_int_or(raw.get("max_chars"), dosage.DEFAULT_MAX_CHARS),
                    redundancy_threshold=_unit_float_or(
                        raw.get("redundancy_threshold"), dosage.DEFAULT_REDUNDANCY_THRESHOLD),
                    reliability_weight=_unit_float_or(raw.get("reliability_weight"), 0.0),
                    recency_weight=_unit_float_or(raw.get("recency_weight"), 0.0),
                    fusion=(
                        str(raw["fusion"])
                        if raw.get("fusion") in FUSIONS
                        else FUSION_NONE if raw.get("fusion")
                        else _unchosen_fusion(root)
                    ),
                    rrf_k=max(1, _int_or(raw.get("rrf_k"), retrieval.DEFAULT_RRF_K)),
                    harm_policy=(
                        str(raw.get("harm_policy"))
                        if raw.get("harm_policy") in harm.POLICIES
                        else harm.POLICY_INFORM
                    ),
                    rerank=(
                        str(raw.get("rerank"))
                        if raw.get("rerank") in RERANKS
                        else _unchosen_rerank(root)
                    ),
                    configured_at=str(raw.get("configured_at") or ""),
                    note=str(raw.get("note") or ""),
                )
        except (OSError, ValueError):
            pass

    logged = _last_logged_settings(root)
    if logged is not None:
        label, floor = logged
        scorer, fusion = parse_eligibility_label(label)
        return RetrievalConfig(
            scorer=scorer,
            floor=floor,
            fusion=fusion,
            rerank=parse_rerank_label(label)[1],
            pinned_for_running_experiment=True,
        )
    if has_recorded_assignments(root):
        return RetrievalConfig(
            scorer=retrieval.SCORER_COUNT,
            floor=0.0,
            pinned_for_running_experiment=True,
        )
    return RetrievalConfig(fusion=default_fusion(), rerank=default_rerank())


def configure(root: str, *, scorer: str | None = None, floor: float | None = None,
              fusion: str | None = None, max_lessons: int | None = None,
              max_chars: int | None = None, redundancy_threshold: float | None = None,
              reliability_weight: float | None = None, recency_weight: float | None = None,
              harm_policy: str | None = None, rerank: str | None = None,
              note: str = "") -> RetrievalConfig:
    """Persist this store's retrieval settings. Returns the new settings."""
    current = load_config(root)
    new_scorer = current.scorer if scorer is None else scorer
    if new_scorer not in retrieval.LEXICAL_SCORERS:
        raise ValueError(
            f"unknown scorer {new_scorer!r}: expected one of "
            f"{', '.join(repr(s) for s in retrieval.LEXICAL_SCORERS)}"
        )
    if floor is not None:
        new_floor = float(floor)
    elif new_scorer != current.scorer:
        new_floor = retrieval.default_floor(new_scorer)
    else:
        new_floor = current.floor
    if not 0.0 <= new_floor <= 1.0:
        raise ValueError(f"relevance floor must be in [0.0, 1.0], got {new_floor}")
    new_fusion = current.fusion if fusion is None else fusion
    if new_fusion not in FUSIONS:
        raise ValueError(
            f"unknown fusion mode {new_fusion!r}: expected one of "
            f"{', '.join(repr(f) for f in FUSIONS)}"
        )
    new_redundancy = (
        current.redundancy_threshold if redundancy_threshold is None
        else float(redundancy_threshold)
    )
    if not 0.0 <= new_redundancy <= 1.0:
        raise ValueError(
            f"redundancy threshold must be in [0.0, 1.0] (0 disables it), "
            f"got {new_redundancy}"
        )
    new_reliability_weight = (
        current.reliability_weight if reliability_weight is None else float(reliability_weight)
    )
    if not 0.0 <= new_reliability_weight <= 1.0:
        raise ValueError(
            f"reliability weight must be in [0.0, 1.0] (0 disables it), "
            f"got {new_reliability_weight}"
        )
    new_recency_weight = (
        current.recency_weight if recency_weight is None else float(recency_weight)
    )
    if not 0.0 <= new_recency_weight <= 1.0:
        raise ValueError(
            f"recency weight must be in [0.0, 1.0] (0 disables it), "
            f"got {new_recency_weight}"
        )
    new_rerank = current.rerank if rerank is None else rerank
    if new_rerank not in RERANKS:
        raise ValueError(
            f"unknown reranker {new_rerank!r}: expected one of "
            f"{', '.join(repr(r) for r in RERANKS)}"
        )
    new_harm_policy = current.harm_policy if harm_policy is None else harm_policy
    if new_harm_policy not in harm.POLICIES:
        raise ValueError(
            f"unknown harm policy {new_harm_policy!r}: expected one of "
            f"{', '.join(repr(p) for p in harm.POLICIES)}"
        )

    config = RetrievalConfig(
        scorer=new_scorer,
        floor=new_floor,
        fusion=new_fusion,
        rrf_k=current.rrf_k,
        max_lessons=(
            current.max_lessons if max_lessons is None else max(0, int(max_lessons))),
        max_chars=(
            current.max_chars if max_chars is None else max(0, int(max_chars))),
        redundancy_threshold=new_redundancy,
        reliability_weight=new_reliability_weight,
        recency_weight=new_recency_weight,
        harm_policy=new_harm_policy,
        rerank=new_rerank,
        configured_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        note=note or current.note,
    )
    from commontrace import frontmatter

    os.makedirs(paths.memory_dir(root), exist_ok=True)
    target = config_path(root)
    with frontmatter.locked(target):
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(target) or ".",
            prefix=CONFIG_NAME + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(
                    {
                        "scorer": config.scorer,
                        "floor": config.floor,
                        "fusion": config.fusion,
                        "rrf_k": config.rrf_k,
                        "max_lessons": config.max_lessons,
                        "max_chars": config.max_chars,
                        "redundancy_threshold": config.redundancy_threshold,
                        "reliability_weight": config.reliability_weight,
                        "recency_weight": config.recency_weight,
                        "harm_policy": config.harm_policy,
                        "rerank": config.rerank,
                        "configured_at": config.configured_at,
                        "note": config.note,
                    },
                    fh,
                    indent=2,
                )
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return config
