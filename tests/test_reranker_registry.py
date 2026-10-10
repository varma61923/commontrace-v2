"""Named second-stage rerankers: registry, MMR and the retrieval seam."""
import pytest

from commontrace import providers, retrieval
from commontrace.exceptions import CapabilityError, ConfigurationError
from commontrace.retrieval import RankedLesson


def _lesson(slug, score, description, terms=()):
    return RankedLesson(path=f"/nowhere/{slug}.md", slug=slug, description=description, score=score,
                        matched_terms=list(terms))


RANKED = [
    _lesson("retry-a", 1.0, "retry the upload with exponential backoff jitter", ["retry", "backoff"]),
    _lesson("retry-b", 0.95, "retry upload exponential backoff with jitter", ["retry", "backoff"]),
    _lesson("idempotency", 0.9, "use an idempotency key for payment requests", ["idempotency"]),
]


def test_builtins_are_listed_after_first_use():
    providers.register_builtin_rerankers()
    assert {"mmr", "cross-encoder", "cross-encoder-fast"} <= set(providers.RERANKERS.names())


def test_mmr_promotes_a_distinct_lesson_over_a_near_duplicate():
    out = retrieval.apply_reranker("upload failed", RANKED, providers.reranker("mmr"))
    assert [r.slug for r in out] == ["retry-a", "idempotency", "retry-b"]
    assert sorted(r.slug for r in out) == sorted(r.slug for r in RANKED)


def test_mmr_with_lambda_one_is_first_stage_order():
    out = providers.mmr_reranker(1.0)("upload failed", RANKED)
    assert [r.slug for r in out] == [r.slug for r in RANKED]


def test_mmr_rejects_an_invalid_lambda():
    with pytest.raises(ConfigurationError):
        providers.mmr_reranker(1.5)


def test_unknown_names_are_a_capability_error_not_an_import():
    with pytest.raises(CapabilityError):
        providers.reranker("os.system")


def test_custom_registration_and_duplicate_refusal():
    name = "reverse-for-test"
    if name not in providers.RERANKERS.names():
        providers.register_reranker(name, lambda: (lambda task, ranked: list(reversed(ranked))))
    assert [r.slug for r in retrieval.apply_reranker("q", RANKED, providers.reranker(name))] == ["idempotency", "retry-b", "retry-a"]
    with pytest.raises(ConfigurationError):
        providers.register_reranker(name, lambda: None)


def test_none_and_callables_still_work():
    assert retrieval.apply_reranker("q", RANKED, None) is RANKED
    assert retrieval.apply_reranker("q", RANKED, lambda t, r: r[:1]) == RANKED[:1]
