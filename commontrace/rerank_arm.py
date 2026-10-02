"""Second-stage reranking with a cross-encoder, opt-in per store."""

from __future__ import annotations

import contextlib
import importlib.util
import threading
from collections.abc import Callable, Mapping, Sequence

MODELS = {
    "cross-encoder": ("cross-encoder/ms-marco-MiniLM-L-6-v2", "minilm6"),
    "cross-encoder-fast": ("cross-encoder/ms-marco-TinyBERT-L-2-v2", "tinybert2"),
}
DEFAULT_MODE = "cross-encoder"

GATE_THRESHOLDS = {
    ("cross-encoder", ""): -4.0,
    ("cross-encoder-fast", ""): -4.0,
    ("cross-encoder", "arctic-m"): -8.0,
    ("cross-encoder-fast", "arctic-m"): -8.0,
}
POOL = 30
POOL_DEPTHS = {
    ("cross-encoder", "arctic-m"): 10,
    ("cross-encoder-fast", "arctic-m"): 15,
}
MAX_CHARS = 1200

_LOCK = threading.Lock()
_LOADED: dict[str, object] = {}
_USE_WORKER = False


def use_worker(enabled: bool = True) -> None:
    """Score through the warm worker when one can be had (see _USE_WORKER)."""
    global _USE_WORKER
    _USE_WORKER = enabled


def _worker_scores(mode: str, pairs: list[tuple[str, str]]) -> list[float] | None:
    if not _USE_WORKER:
        return None
    from commontrace import warm

    return warm.rerank_scores(mode, pairs)


def available() -> bool:
    """Whether sentence-transformers is installed (the attention extra)."""
    return importlib.util.find_spec("sentence_transformers") is not None


def tag(mode: str) -> str:
    return MODELS[mode][1]


def mode_for_tag(model_tag: str) -> str | None:
    return next((m for m, (_name, t) in MODELS.items() if t == model_tag), None)


@contextlib.contextmanager
def _no_progress_bars():
    restore = []
    try:
        from huggingface_hub import utils as hub_utils

        if not hub_utils.are_progress_bars_disabled():
            hub_utils.disable_progress_bars()
            restore.append(hub_utils.enable_progress_bars)
    except Exception:  # noqa: BLE001 - an older library: leave its bars alone
        pass
    try:
        from transformers.utils import logging as transformers_logging

        if transformers_logging.is_progress_bar_enabled():
            transformers_logging.disable_progress_bar()
            restore.append(transformers_logging.enable_progress_bar)
    except Exception:  # noqa: BLE001 - an older library: leave its bars alone
        pass
    try:
        yield
    finally:
        for enable in restore:
            enable()


def _load(mode: str = DEFAULT_MODE):
    if mode not in _LOADED:
        from sentence_transformers import CrossEncoder

        try:
            with _no_progress_bars():
                _LOADED[mode] = CrossEncoder(MODELS[mode][0], device="cpu", local_files_only=True)
        except Exception:  # noqa: BLE001 - not cached (or an older library): fetch it
            _LOADED[mode] = CrossEncoder(MODELS[mode][0], device="cpu")
    return _LOADED[mode]


def ready(mode: str = DEFAULT_MODE) -> str:
    """Load the model now; "" when it can rerank, else why not."""
    if not available():
        return "the reranker needs the attention extra (`pip install commontrace[attention]`)"
    if _worker_scores(mode, []) is not None:
        return ""
    try:
        with _LOCK:
            _load(mode)
    except Exception as exc:  # noqa: BLE001 - no model is a fallback, never a crash
        return f"the reranker could not load {MODELS[mode][0]}: {type(exc).__name__}: {exc}"
    return ""


def texts(slugs: Sequence[str], path_of: Mapping[str, str], read) -> dict[str, str]:
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
    parts = [
        str(frontmatter.get("description") or ""),
        str(frontmatter.get("applies_when") or ""),
        body or "",
    ]
    return " ".join(p.strip() for p in parts if p and p.strip())[:MAX_CHARS]


def pool_size(want: int, mode: str | None = None, embedder: str = "") -> int:
    return max(want, POOL_DEPTHS.get((mode, embedder), POOL))


def gate_threshold(mode: str, embedder: str = "") -> float:
    return GATE_THRESHOLDS.get((mode, embedder), GATE_THRESHOLDS[(mode, "")])


def admit_gated(
    floor_cleared: set[str], mode: str = DEFAULT_MODE, embedder: str = "",
) -> Callable[[str, float], bool]:
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
    """Reorder `pool` by cross-encoder score and keep the top `want`."""
    candidates = [s for s in pool if s in text_of]
    extra = [s for s in withdrawn if s in text_of and s not in set(candidates)]
    if not candidates and not extra:
        return [], []
    pairs = [(task, text_of[s][:MAX_CHARS]) for s in candidates + extra]
    scores = _worker_scores(mode, pairs)
    if scores is None:
        with _LOCK:
            model = _load(mode)
            scores = model.predict(pairs, batch_size=64, show_progress_bar=False)
    scored = [(s, float(x)) for s, x in zip(candidates, scores[: len(candidates)])]
    if admit is not None:
        scored = [(s, x) for s, x in scored if admit(s, x)]
    order = sorted(range(len(scored)), key=lambda i: (-scored[i][1], i))
    page = [scored[i] for i in order[:want]]
    on_page = []
    for slug, score in zip(extra, scores[len(candidates):]):
        if admit is not None and not admit(slug, float(score)):
            continue
        above = sum(1 for _s, x in scored if x > float(score))
        if above < want:
            on_page.append(slug)
    return page, on_page
