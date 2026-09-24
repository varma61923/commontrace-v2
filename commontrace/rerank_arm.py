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

HARM WITHDRAWAL stays exact. A withdrawn lesson is scored alongside the pool
and named only if its score would have put it on the page. A cross-encoder
scores each pair on its own, so leaving it out moves nothing else.
"""

from __future__ import annotations

import importlib.util
import threading
from collections.abc import Mapping, Sequence

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


def rerank(
    task: str,
    pool: Sequence[str],
    text_of: Mapping[str, str],
    want: int,
    withdrawn: Sequence[str] = (),
    mode: str = DEFAULT_MODE,
) -> tuple[list[tuple[str, float]], list[str]]:
    """Reorder `pool` by cross-encoder score and keep the top `want`.

    Returns (page, withdrawn_on_page): the page as (slug, score) in rank
    order, and those of `withdrawn` whose score would have placed them on
    it. Ties keep first-stage order. Raises whatever the model raises; the
    caller decides how to fall back.
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
    order = sorted(range(len(scored)), key=lambda i: (-scored[i][1], i))
    page = [scored[i] for i in order[:want]]
    on_page = []
    for slug, score in zip(extra, scores[len(candidates):]):
        # Its position among the pool, had it stayed in: the lessons that
        # outscore it go above it.
        above = sum(1 for _s, x in scored if x > float(score))
        if above < want:
            on_page.append(slug)
    return page, on_page
