"""One store's retrieval settings, read by EVERY retriever.

Modeled deliberately on commontrace/holdout_io.py's ExperimentConfig, for the
same reason it exists: a setting that lives as a CLI flag default on `query`
and a separate constant in the MCP server is a setting the two surfaces can
silently disagree about. Retrieval settings are worse in that respect than the
holdout rate, because they decide which lessons are ELIGIBLE at all -- so a
disagreement between surfaces doesn't just change what an agent sees, it
changes the denominator of the causal experiment measuring whether any of it
helped (commontrace/integrity.py's check_scorer_drift).

THE BACKWARD-COMPATIBILITY RULE HERE IS LOAD-BEARING. A store that already has
holdout assignments on disk was scored by the historical raw-additive scorer
with no floor. Switching it to IDF scoring mid-flight would change which
lessons clear the bar, which changes eligibility, which makes the assignments
before and after the upgrade two different experiments -- pooled into one
comparison, silently, by the act of upgrading. holdout_io.read_log's own
comment on the `salt` field states the principle:

    "Backward compatibility for a measurement is not a nicety: the
     alternative is a fleet's entire experiment history becoming unreadable
     on upgrade."

So `load_config` pins such a store to the historical scorer and prints how to
opt in, rather than upgrading it silently. A store with no experiment running
has nothing to invalidate and gets the better scorer immediately.
"""

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

#: How the arms are combined. "none" is the historical either/or: semantic
#: when the extra is installed and the index is fresh, lexical otherwise.
FUSION_NONE = "none"
#: Reciprocal Rank Fusion over both arms (commontrace/retrieval.py). Position
#: only, because cosine and IDF relevance are not on a comparable scale.
FUSION_RRF = "rrf"
#: Both arms feed the reranker, but a candidate that did not clear the lexical
#: relevance floor reaches the page only if the cross-encoder vouches for it
#: (commontrace/rerank_arm.py, GATE_THRESHOLDS). Floor-cleared lessons are
#: admitted exactly as without fusion, so, unlike `rrf`, the page is never
#: filled with lessons the task is not about. Needs a reranker.
FUSION_GATED = "gated"
FUSIONS = (FUSION_NONE, FUSION_RRF, FUSION_GATED)

#: The label an assignment records when the semantic arm ALONE decided
#: eligibility -- `commontrace query` with fusion=none and the attention
#: extra installed. It used to record no label at all, and
#: integrity.check_scorer_drift skips unlabeled rows, so a store whose
#: occasions flipped between this path and lexical (a stale index, a
#: missing extra) pooled two treatments with nothing to show it.
SEMANTIC_ONLY = "semantic"

#: Second-stage reranking of the first stage's candidates
#: (commontrace/rerank_arm.py). "none" keeps the first stage's order.
RERANK_NONE = "none"
#: A cross-encoder reads the task and each candidate together and reorders
#: the pool by that score (commontrace/rerank_arm.py's MODELS).
RERANK_CE = "cross-encoder"
#: The same, with a model about ten times faster and less accurate.
RERANK_CE_FAST = "cross-encoder-fast"
RERANKS = (RERANK_NONE, RERANK_CE, RERANK_CE_FAST)

#: Overrides the reranker a store gets when it has not chosen one
#: (`default_rerank`). For operators who want no model download, and for
#: test suites that must not depend on which extras are installed.
DEFAULT_RERANK_ENV = "COMMONTRACE_DEFAULT_RERANK"


#: Overrides the fusion mode a store gets when it has not chosen one
#: (`default_fusion`), e.g. to keep the reranker but skip the semantic index.
DEFAULT_FUSION_ENV = "COMMONTRACE_DEFAULT_FUSION"


def default_fusion() -> str:
    """The fusion mode a store gets when it has not chosen one and has no
    experiment history: gated fusion wherever the default reranker runs
    (it needs one), else none.

    Gated fusion passes every gate the default ranking is held to: a lesson
    that clears the lexical floor is admitted exactly as before, and anything
    else only when the cross-encoder vouches for it, so on the curated
    fixture every field keeps its recall and collateral unchanged. On LoCoMo
    it lifts R@5 from 0.562 to 0.598 and R@10 from 0.615 to 0.669 over
    reranked lexical retrieval (0.608 / 0.694 with the arctic-embed arm a
    new index uses). Its cost is the semantic index: the first retrieval
    embeds the store's lessons, later ones only what changed.
    """
    override = os.environ.get(DEFAULT_FUSION_ENV, "")
    if override in FUSIONS:
        return override
    from commontrace import semantic_arm

    if default_rerank() != RERANK_NONE and semantic_arm.available():
        return FUSION_GATED
    return FUSION_NONE


def default_rerank() -> str:
    """The reranker a store gets when it has not chosen one and has no
    experiment history to stay consistent with.

    The fast cross-encoder when the attention extra is installed, else none.
    It passes every gate the default ranking is held to: it reorders only
    lessons that already cleared the relevance floor, onto a page no longer
    than the lexical one, so on the curated fixture it finds every relevant
    lesson with exactly the lexical default's collateral in every field --
    and puts the right one first more often. On LoCoMo it lifts R@5 from
    0.472 to 0.562 and MRR from 0.387 to 0.518 (LoCoMo), for about
    30 ms per retrieval. Fusion stays opt-in: it fills every slot on the
    page, which the fixture's collateral ceiling rejects
    (commontrace/rerank_arm.py).
    """
    override = os.environ.get(DEFAULT_RERANK_ENV, "")
    if override in RERANKS:
        return override
    from commontrace import rerank_arm

    return RERANK_CE_FAST if rerank_arm.available() else RERANK_NONE

# WHY THE RECORDED SCORER CARRIES THE ARM COMPOSITION
# ---------------------------------------------------
# The holdout log records `scorer` and `floor` as the evidence of what decided
# ELIGIBILITY, and integrity.check_scorer_drift invalidates an experiment
# whose scorer changed mid-run -- because "injected when retrieved" is not one
# treatment if what counts as retrieved moved.
#
# Turning fusion on moves exactly that. A lesson no lexical pass would surface
# becomes eligible because the semantic arm ranked it, and the denominator of
# the experiment changes with it. Recording the composition INSIDE the scorer
# label means the existing drift check catches it for free -- no second column
# that an older reader would ignore, and no second check that could disagree
# with the first about the same fact.
#
# The semantic arm's embedding model moves it too, so a fused label names the
# model after an `@` -- except the original model, whose label is unchanged,
# so a store that has logged under it reads as the same treatment it always
# was.
_FUSION_LABEL = re.compile(
    r"^(?P<mode>rrf|gated)\((?P<lexical>[^+()@]+)\+semantic(?:@(?P<embedder>[\w.-]+))?\)$")

#: The short name each trusted embedding model (commontrace/reference/
#: query.py's TRUSTED_MODELS) records in a fused label. "" is the original
#: model, recorded as no name at all.
EMBEDDER_TAGS = {
    "multi-qa-mpnet-base-dot-v1": "",
    "Snowflake/snowflake-arctic-embed-m-v1.5": "arctic-m",
}


def embedder_tag(model_name: str | None) -> str:
    """The label's name for `model_name`; "" for the original model or an
    unknown one (the index loader refuses an untrusted model anyway)."""
    return EMBEDDER_TAGS.get(model_name or "", "")
# A reranked ranking wraps the first stage's label with the model that
# reordered it: a different model is a different treatment.
_RERANK_LABEL = re.compile(r"^ce:(?P<model>[^()]+)\((?P<inner>.+)\)$")


def eligibility_label(
    scorer: str, fusion: str, rerank: str = RERANK_NONE, embedder: str = "",
) -> str:
    """What to record as the `scorer` of an assignment made under these
    settings. `embedder` is the semantic arm's `embedder_tag`."""
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

        # A model this build does not know is still a reranked ranking; it
        # is pinned to the default model rather than read as unreranked.
        return match.group("inner"), rerank_arm.mode_for_tag(match.group("model")) or RERANK_CE
    return label, RERANK_NONE


def semantic_only_label(embedder: str = "") -> str:
    """The label of a ranking the semantic arm decided alone, naming its
    embedder as a fused label does."""
    return f"{SEMANTIC_ONLY}@{embedder}" if embedder else SEMANTIC_ONLY


def _is_semantic_only(label: str) -> bool:
    return label == SEMANTIC_ONLY or (label or "").startswith(SEMANTIC_ONLY + "@")


def parse_embedder(label: str) -> str:
    """The embedder tag a recorded label names ("" for the original model or
    a label with no semantic arm)."""
    label = parse_rerank_label(label)[0] or ""
    if _is_semantic_only(label):
        return label.partition("@")[2]
    match = _FUSION_LABEL.match(label)
    return (match.group("embedder") or "") if match else ""


def parse_eligibility_label(label: str) -> tuple[str, str]:
    """Inverse of `eligibility_label`: (lexical scorer, fusion mode).

    A label this build does not recognise is read as a plain scorer with no
    fusion, which is what every pre-fusion log line is. A reranker's wrapper
    is looked through (`parse_rerank_label` reads it).
    """
    label, _rerank = parse_rerank_label(label)
    match = _FUSION_LABEL.match(label or "")
    if match:
        return match.group("lexical"), match.group("mode")
    if _is_semantic_only(label):
        # No lexical scorer decided eligibility; the lexical fallback is the
        # default one.
        return retrieval.SCORER_IDF, FUSION_NONE
    return label, FUSION_NONE


def config_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), CONFIG_NAME)


@dataclass(frozen=True)
class RetrievalConfig:
    scorer: str = retrieval.SCORER_IDF
    floor: float = retrieval.DEFAULT_FLOOR
    # How much retrieved memory actually reaches the agent
    # (commontrace/dosage.py). `floor` and `scorer` decide WHICH lessons are
    # eligible; these decide how many of them fit. Kept here, with the rest
    # of the retrieval settings, because a store whose budget differs is
    # serving a different treatment -- the same reason scorer and floor are
    # read from the store rather than passed per call.
    max_lessons: int = dosage.DEFAULT_MAX_LESSONS
    max_chars: int = dosage.DEFAULT_MAX_CHARS
    #: Similarity (commontrace/redundancy.py's token-set Jaccard) at or above
    #: which a lesson competing for the budget is dropped for restating one
    #: already admitted. 0.0 (the default) disables the check entirely --
    #: see commontrace/dosage.py's module docstring for why suppression is
    #: opt-in rather than an upgrade side effect.
    redundancy_threshold: float = dosage.DEFAULT_REDUNDANCY_THRESHOLD
    #: Whether to fuse the lexical and semantic arms rather than pick one.
    #: Defaults to the historical either/or, because switching a store that
    #: is mid-experiment would change its eligibility denominator -- opting
    #: in is a decision, not an upgrade side effect.
    fusion: str = FUSION_NONE
    #: RRF's rank-damping constant. Exposed because commons/eval sweeps it.
    rrf_k: int = retrieval.DEFAULT_RRF_K
    #: How much a lesson's measured track record (commontrace/reliability.py)
    #: moves its rank among lessons that already cleared `floor`. 0.0 (the
    #: default) is off: this does not change which lessons are ELIGIBLE
    #: (see retrieval.rank_lessons's own docstring on that boundary), only
    #: their order, and "opting in is a decision" applies here for the same
    #: reason it applies to `redundancy_threshold` above -- a store mid
    #: experiment that starts reordering by reliability has changed which
    #: of its eligible lessons the budget actually admits, even though the
    #: eligible SET itself is untouched.
    reliability_weight: float = 0.0
    #: How much a lesson's `last_hit` freshness (commontrace/recency.py)
    #: moves its rank among lessons that already cleared `floor`. Same
    #: default-off reasoning as `reliability_weight`.
    recency_weight: float = 0.0
    #: What retrieval does with a lesson the experiment measured making
    #: outcomes worse (commontrace/harm.py): "inform" (the default) attaches
    #: the verdict and still injects it; "withdraw" stops injecting it and
    #: names it instead. Default-off for the reason every setting here that
    #: changes what a running fleet is given is.
    harm_policy: str = harm.POLICY_INFORM
    #: Second-stage reranking (commontrace/rerank_arm.py). Off by default: it
    #: changes which lessons make the top k, so turning it on is a new
    #: treatment, and it needs the attention extra.
    rerank: str = RERANK_NONE

    @property
    def eligibility(self) -> str:
        """The label an assignment made under these settings records."""
        return eligibility_label(self.scorer, self.fusion, self.rerank)

    def eligibility_label_for(self, *, fused: bool, embedder: str = "") -> str:
        """The FIRST stage's label as it actually ran: fused (in this
        store's fusion mode, with the semantic arm's `embedder` tag) only if
        the semantic arm did run. A reranker's wrapper is added by the
        caller, only if it ran (`rerank_label`)."""
        if not fused:
            return eligibility_label(self.scorer, FUSION_NONE)
        return eligibility_label(self.scorer, self.fusion, embedder=embedder)
    configured_at: str = ""
    note: str = ""
    # True when these settings were inferred for an existing store rather than
    # chosen by anyone, so callers can say so once instead of pretending the
    # store opted into them.
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
    # NaN/Inf parse but poison every comparison (x >= nan is False):
    # a nan floor would silently yield empty results. Fall back instead.
    if not math.isfinite(out):
        return default
    return out


def _unit_float_or(value: object, default: float) -> float:
    """Like `_float_or`, additionally rejecting anything outside [0, 1] --
    every field this validates (`redundancy_threshold`, `reliability_weight`,
    `recency_weight`) is either raised on out-of-range by its own consumer
    (`dosage.Budget`) or documented as meaningless outside it, and a
    malformed config must fall back to the default rather than crash the
    retrieval that reads it (the same posture `load_config`'s own docstring
    states for the file as a whole)."""
    out = _float_or(value, default)
    return out if 0.0 <= out <= 1.0 else default


def has_recorded_assignments(root: str) -> bool:
    """Whether this store has already logged holdout assignments.

    Checked by size rather than by parsing: this runs on every retrieval, and
    the question is only "is there history here to protect".
    """
    from commontrace import holdout_io

    path = holdout_io.holdout_log_path(root)
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _last_logged_settings(root: str) -> tuple[str, float] | None:
    """The scorer and floor the most recent assignment was made under.

    None when there is no log, or when the last line predates these fields --
    which is what identifies a genuine pre-upgrade store.

    Reads only the tail of the file. This runs on every retrieval, and a
    fleet's log grows without bound, so parsing all of it here would make
    retrieval get slower the longer the pilot runs.
    """
    from commontrace import holdout_io

    path = holdout_io.holdout_log_path(root)
    try:
        size = os.path.getsize(path)
        if size == 0:
            return None
        with open(path, "rb") as fh:
            # A logged row is a few hundred bytes; 8 KiB covers the last one
            # comfortably without reading a large log into memory.
            fh.seek(max(0, size - 8192))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed([ln for ln in tail.splitlines() if ln.strip()]):
        try:
            raw = json.loads(line)
        except ValueError:
            continue  # a torn final line, or a partial first line from the seek
        scorer = raw.get("scorer")
        floor = raw.get("floor")
        if scorer and floor is not None:
            try:
                return str(scorer), float(floor)
            except (TypeError, ValueError):
                return None
        return None  # a complete row that simply predates these fields
    return None


def logged_embedding_model(root: str) -> str | None:
    """The embedding model this store's most recent assignment was ranked
    with, when the semantic arm took part in it; None otherwise.

    What an index rebuilt from nothing (deleted, or never built on this
    machine) is built with, so a store mid-experiment keeps its semantic
    arm's model rather than taking the default. An index that holds lessons
    already names its model, and keeps it (build_index.index_model).
    """
    logged = _last_logged_settings(root)
    if logged is None:
        return None
    if (parse_eligibility_label(logged[0])[1] == FUSION_NONE
            and not _is_semantic_only(parse_rerank_label(logged[0])[0])):
        return None
    tag = parse_embedder(logged[0])
    return next((name for name, t in EMBEDDER_TAGS.items() if t == tag), None)


def _unchosen_fusion(root: str) -> str:
    """The fusion mode for a store whose settings never named one: what its
    log says it ran, else the default (the same rule as `_unchosen_rerank`)."""
    logged = _last_logged_settings(root)
    if logged is not None:
        return parse_eligibility_label(logged[0])[1]
    if has_recorded_assignments(root):
        return FUSION_NONE
    return default_fusion()


def _unchosen_rerank(root: str) -> str:
    """The reranker for a store whose settings never named one.

    What its log says it ran, when it has one: the reranker decides
    eligibility, so a store mid-experiment must not gain one on upgrade (or
    lose one when its settings are next read). Otherwise the default.
    """
    logged = _last_logged_settings(root)
    if logged is not None:
        return parse_rerank_label(logged[0])[1]
    if has_recorded_assignments(root):
        return RERANK_NONE
    return default_rerank()


def load_config(root: str) -> RetrievalConfig:
    """This store's retrieval settings, or the right defaults if unset.

    Never raises. A malformed config falls back to defaults rather than
    failing the retrieval that asked for it -- refusing to serve a lesson
    because a settings file is corrupt trades a working fleet for a tidy
    error (holdout_io.load_config takes the same position).
    """
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
                    # The scorer's own default when unset: each scorer's
                    # relevance sits on its own scale (retrieval.default_floor).
                    floor=_float_or(raw.get("floor"), retrieval.default_floor(scorer)),
                    # Absent in a config written before budgets existed,
                    # which is the overwhelmingly common case: such a store
                    # gets the defaults rather than zero, because a budget
                    # of nothing would silently stop injecting anything.
                    max_lessons=_int_or(
                        raw.get("max_lessons"), dosage.DEFAULT_MAX_LESSONS),
                    max_chars=_int_or(raw.get("max_chars"), dosage.DEFAULT_MAX_CHARS),
                    redundancy_threshold=_unit_float_or(
                        raw.get("redundancy_threshold"), dosage.DEFAULT_REDUNDANCY_THRESHOLD),
                    reliability_weight=_unit_float_or(raw.get("reliability_weight"), 0.0),
                    recency_weight=_unit_float_or(raw.get("recency_weight"), 0.0),
                    # An unrecognised value reads as "no fusion" rather than
                    # raising: this file is read on every retrieval, and a
                    # typo must not stop a fleet retrieving.
                    fusion=(
                        str(raw["fusion"])
                        if raw.get("fusion") in FUSIONS
                        else FUSION_NONE if raw.get("fusion")
                        else _unchosen_fusion(root)
                    ),
                    rrf_k=max(1, _int_or(raw.get("rrf_k"), retrieval.DEFAULT_RRF_K)),
                    # Same posture as fusion: an unrecognised value reads as
                    # the default rather than stopping retrieval.
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

    # Unconfigured. A store with assignments already on disk keeps the scorer
    # those assignments were made under; see this module's docstring.
    #
    # WHICH scorer that is has to come from the log, not from the mere
    # EXISTENCE of a log. Treating "there are assignments" as "this is a
    # pre-upgrade store" was wrong in the case that matters most: a brand-new
    # store writes its first assignment under the current scorer, and from the
    # second query onward the log exists -- so every new fleet was silently
    # downgraded to the historical scorer after one query, and its log then
    # held two scorers, which check_scorer_drift correctly reports as an
    # INVALIDATED experiment. The mechanism meant to protect an upgrade was
    # breaking every fresh pilot instead.
    #
    # New rows record `scorer`/`floor`; pre-upgrade rows do not. That
    # distinction is exactly the question being asked, so ask it directly.
    logged = _last_logged_settings(root)
    if logged is not None:
        label, floor = logged
        # The logged label may carry the arm composition; restoring only the
        # lexical half would silently drop the semantic arm and change the
        # denominator in the direction this pinning exists to prevent.
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
    """Persist this store's retrieval settings. Returns the new settings.

    EVERY setting is carried through from the current config, not just the
    ones this call changes. Writing only the named fields meant a store that
    had set a context budget lost it the next time anyone touched the floor
    -- the budget silently reverted to the default, so an operator tightening
    precision by one flag also tripled how much text their agents received,
    with nothing printed. A partial write is the wrong shape for a settings
    file that more than one command edits.

    Callers that change `scorer`, `floor`, `fusion` or `rerank` on a store with a
    running experiment must rotate the holdout salt afterwards
    (holdout_io.configure): all three change which lessons are eligible, so
    the assignments before and after describe two different treatments,
    exactly as a changed holdout rate does.
    """
    current = load_config(root)
    new_scorer = current.scorer if scorer is None else scorer
    if new_scorer not in retrieval.LEXICAL_SCORERS:
        raise ValueError(
            f"unknown scorer {new_scorer!r}: expected one of "
            f"{', '.join(repr(s) for s in retrieval.LEXICAL_SCORERS)}"
        )
    # A scorer change without an explicit floor takes the new scorer's own
    # default: relevance sits on a different scale under each, so carrying
    # the old floor across would silently mean a different cut.
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
    # Atomic + locked + fsynced, matching holdout_io.configure: the previous
    # fixed ".tmp" name without a lock raced concurrent configures and a
    # crash mid-write lost the config. Unique tmp via mkstemp, lock the
    # target, fsync before replace.
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
