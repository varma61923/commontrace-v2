"""Second-stage reranking with a cross-encoder, opt-in per store.

Both first-stage arms score the task and each lesson SEPARATELY: the lexical
arm counts shared rare words, the semantic arm compares two vectors encoded
apart. A cross-encoder reads the task and the lesson TOGETHER and scores the
pair, which is why it ranks better and why it is too slow to run over a
whole store. So it runs over a short candidate pool the first stage already
found (`POOL`), and only reorders it.

Measured on LoCoMo's 1,531 questions (benchmark/peers/), reranking the
fused pool raised the share of questions with an answering turn in the top
5 from 53.1% to 67.0%, and MRR from 0.440 to 0.626. Reranking the lexical
arm alone, with no embedding model at all, reaches 59.7%. mem0 2.x reaches
54.3%.

WHAT IT DOES NOT CHANGE. The pool is the first stage's output, after the
relevance floor, `exclude_shown` and core-lesson handling, so the reranker
can only reorder lessons the store would already consider. It never adds
one.

EXPERIMENTS. The reranker decides which lessons make the top k, so it
decides eligibility: a store that turns it on starts a new treatment, and
assignments record `ce:<model>(<first stage>)` as their label
(retrieval_io.eligibility_label), so the audit sees the change rather than
pooling two rankings.

WHY IT IS NOT THE DEFAULT. On the curated lesson fixture (eight fields,
top 3; commontrace/reference/measure_retrieval.py) the default is held to
finding every relevant lesson with collateral under 2.4x, because every
lesson retrieved is logged into the causal experiment. Fused retrieval
fills every slot on the page, so its pollution is 3.0x in every field
(the lexical default: 1.72-2.33x). A floor on the cross-encoder's score
cuts that to 1.0-1.2x, but no floor keeps recall at 1.0: at -11 one field
still loses a relevant lesson (0.94), and at -3 recall is 0.83. Measured
with both models and both lexical scorers. On conversational benchmarks
reranked fusion is the most accurate stack measured; on a curated store
under experiment, its collateral dilutes the estimates the product
exists to report. So plain fusion is a choice a store makes, not an
upgrade; gated fusion (GATE_THRESHOLDS below) is the fused ranking that
passes those gates, and is the default where the attention extra is
installed.

HARM WITHDRAWAL stays exact. A withdrawn lesson is scored alongside the pool
and named only if its score would have put it on the page. A cross-encoder
scores each pair on its own, so leaving it out moves nothing else.
"""

from __future__ import annotations

import importlib.util
import threading
from collections.abc import Callable, Mapping, Sequence

#: The rerank modes a store can configure (retrieval_io.RERANKS), each a
#: cross-encoder trained on MS MARCO passage ranking, and the short name its
#: eligibility label records: a different model is a different ranking, and
#: so a different treatment. Measured over a 30-candidate pool of LoCoMo
#: turns on a 4-core CPU:
#:
#:   mode                 model (params)          rerank p50   fused R@5 / MRR
#:   cross-encoder        MiniLM-L-6 (22M)        265 ms       0.670 / 0.626
#:   cross-encoder-fast   TinyBERT-L-2 (4M)       28 ms        0.606 / 0.545
#:   (no reranking)                                            0.531 / 0.440
MODELS = {
    "cross-encoder": ("cross-encoder/ms-marco-MiniLM-L-6-v2", "minilm6"),
    "cross-encoder-fast": ("cross-encoder/ms-marco-TinyBERT-L-2-v2", "tinybert2"),
}
DEFAULT_MODE = "cross-encoder"

#: GATED FUSION (retrieval_io.FUSION_GATED). Plain fusion fills every slot on
#: the page with whatever the semantic arm ranked, relevant or not, which the
#: curated fixture's collateral ceiling rejects (below). Gated fusion lets a
#: candidate that did NOT clear the lexical floor onto the page only when the
#: cross-encoder scores it at least this high; floor-cleared lessons are
#: admitted exactly as without fusion. On the fixture the unrelated lessons
#: score -8 to -11 against the tasks, so every field keeps its recall and
#: collateral unchanged at any threshold down to -6 (measured, both models).
#: On LoCoMo, at -4: R@5 0.598 and R@10 0.669 with the fast model (lexical +
#: fast rerank: 0.562 / 0.615; mem0 2.x: 0.543 / 0.625), and 0.679 / 0.731
#: with the accurate one.
#:
#: The threshold is part of the treatment, so it is set per semantic-arm
#: embedder (retrieval_io.EMBEDDER_TAGS) and a store keeps the one its label
#: names: "" is the original model, whose stores keep -4. With
#: snowflake-arctic-embed-m the semantic arm finds more of what the page
#: needs, and the gate can be looser before the fixture notices: re-measured
#: with that arm, every field keeps recall 1.0 and its collateral down to -9
#: (accurate) and -10 (fast); at -10 and -11 respectively a field's
#: collateral rises. -8 keeps a margin under both. On LoCoMo, at -8: R@5
#: 0.608 / R@10 0.694 fast, 0.703 / 0.762 accurate.
GATE_THRESHOLDS = {
    ("cross-encoder", ""): -4.0,
    ("cross-encoder-fast", ""): -4.0,
    ("cross-encoder", "arctic-m"): -8.0,
    ("cross-encoder-fast", "arctic-m"): -8.0,
}
#: How many first-stage candidates the reranker reorders.
POOL = 30
#: Characters of lesson text the model reads. Its input is capped at 512
#: tokens, shared with the task.
MAX_CHARS = 1200

_LOCK = threading.Lock()
_LOADED: dict[str, object] = {}


def available() -> bool:
    """Whether sentence-transformers is installed (the attention extra)."""
    return importlib.util.find_spec("sentence_transformers") is not None


def tag(mode: str) -> str:
    return MODELS[mode][1]


def mode_for_tag(model_tag: str) -> str | None:
    return next((m for m, (_name, t) in MODELS.items() if t == model_tag), None)


def _load(mode: str = DEFAULT_MODE):
    if mode not in _LOADED:
        from sentence_transformers import CrossEncoder

        _LOADED[mode] = CrossEncoder(MODELS[mode][0], device="cpu")
    return _LOADED[mode]


def ready(mode: str = DEFAULT_MODE) -> str:
    """Load the model now; "" when it can rerank, else why not.

    Checked BEFORE the first stage runs, because the first stage fetches a
    deeper pool for the reranker than it would for the page: a store that
    cannot rerank must rank for the page, exactly as if it had not asked.
    """
    if not available():
        return "the reranker needs the attention extra (`pip install commontrace[attention]`)"
    try:
        with _LOCK:
            _load(mode)
    except Exception as exc:  # noqa: BLE001 - no model is a fallback, never a crash
        return f"the reranker could not load {MODELS[mode][0]}: {type(exc).__name__}: {exc}"
    return ""


def texts(slugs: Sequence[str], path_of: Mapping[str, str], read) -> dict[str, str]:
    """`lesson_text` for each slug, read from its file with `read(path) ->
    (frontmatter, body)`. A lesson that cannot be read is left out, and so
    cannot be reranked onto the page."""
    out: dict[str, str] = {}
    for slug in dict.fromkeys(slugs):
        path = path_of.get(slug)
        if not path:
            continue
        try:
            fm, body = read(path)
        except Exception:  # noqa: BLE001 - an unreadable lesson is skipped, as the page skips it
            continue
        out[slug] = lesson_text(fm, body)
    return out


def lesson_text(frontmatter: Mapping, body: str) -> str:
    """What the cross-encoder reads for one lesson: what it is about, when
    it applies, then the rule itself."""
    parts = [
        str(frontmatter.get("description") or ""),
        str(frontmatter.get("applies_when") or ""),
        body or "",
    ]
    return " ".join(p.strip() for p in parts if p and p.strip())[:MAX_CHARS]


def pool_size(want: int) -> int:
    """How many candidates the first stage should hand over for a page of
    `want`."""
    return max(want, POOL)


def gate_threshold(mode: str, embedder: str = "") -> float:
    """The gate for `mode` with a semantic arm whose embedder tag is
    `embedder`; an embedder this build has no threshold for gets the
    original, strictest one."""
    return GATE_THRESHOLDS.get((mode, embedder), GATE_THRESHOLDS[(mode, "")])


def admit_gated(
    floor_cleared: set[str], mode: str = DEFAULT_MODE, embedder: str = "",
) -> Callable[[str, float], bool]:
    """Gated fusion's admission rule: a lesson the lexical arm scored at or
    above the relevance floor is always admissible, as it is without fusion;
    any other candidate, a semantic-arm find or a below-floor lexical match,
    only if the cross-encoder scores it at least `gate_threshold(mode,
    embedder)`."""
    threshold = gate_threshold(mode, embedder)
    return lambda slug, score: slug in floor_cleared or score >= threshold


def rerank(
    task: str,
    pool: Sequence[str],
    text_of: Mapping[str, str],
    want: int,
    withdrawn: Sequence[str] = (),
    mode: str = DEFAULT_MODE,
    admit: Callable[[str, float], bool] | None = None,
) -> tuple[list[tuple[str, float]], list[str]]:
    """Reorder `pool` by cross-encoder score and keep the top `want`.

    Returns (page, withdrawn_on_page): the page as (slug, score) in rank
    order, and those of `withdrawn` whose score would have placed them on
    it. Ties keep first-stage order. `admit(slug, score)`, when given,
    decides which scored candidates may be on the page at all (gated
    fusion: `admit_gated`); a withdrawn lesson it would not admit is not
    named. Raises whatever the model raises; the caller decides how to fall
    back.
    """
    candidates = [s for s in pool if s in text_of]
    extra = [s for s in withdrawn if s in text_of and s not in set(candidates)]
    if not candidates and not extra:
        return [], []
    with _LOCK:
        model = _load(mode)
        scores = model.predict(
            # Capped here, not only in `lesson_text`, so every caller -- both
            # surfaces and the benchmark -- reranks the same text.
            [(task, text_of[s][:MAX_CHARS]) for s in candidates + extra],
            batch_size=64, show_progress_bar=False,
        )
    scored = [(s, float(x)) for s, x in zip(candidates, scores[: len(candidates)])]
    if admit is not None:
        scored = [(s, x) for s, x in scored if admit(s, x)]
    order = sorted(range(len(scored)), key=lambda i: (-scored[i][1], i))
    page = [scored[i] for i in order[:want]]
    on_page = []
    for slug, score in zip(extra, scores[len(candidates):]):
        if admit is not None and not admit(slug, float(score)):
            continue
        # Its position among the pool, had it stayed in: the lessons that
        # outscore it go above it.
        above = sum(1 for _s, x in scored if x > float(score))
        if above < want:
            on_page.append(slug)
    return page, on_page
