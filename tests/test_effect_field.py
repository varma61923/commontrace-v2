"""Effect field: content-similar memories share randomized evidence, only when it predicts."""

from __future__ import annotations

import datetime
import json
import os
import random
import types
from dataclasses import asdict

import pytest

from unimem import allocation, causal_gate, effect_field, holdout_io, integrity
from unimem.causal_gate import Row
from unimem.cli import main
from unimem.commands import experiment_cmd

T0 = datetime.datetime(2026, 3, 1, 9, 0, tzinfo=datetime.timezone.utc)

FAMILIES = {
    "refund": ("refund payments ledger invoice reconcile settlement", 0.20),
    "legacy": ("legacy endpoint deprecated soap fallback adapter", -0.20),
    "retry": ("retry backoff throttle jitter quota limit", 0.0),
}


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: None)


@pytest.fixture
def clock(monkeypatch):
    state = types.SimpleNamespace(now=T0)

    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return state.now

    frozen = types.SimpleNamespace(datetime=Frozen, timezone=datetime.timezone, timedelta=datetime.timedelta)
    monkeypatch.setattr(holdout_io, "datetime", frozen)
    monkeypatch.setattr(allocation, "datetime", frozen)
    return state


def paraphrase(rng: random.Random, family: str) -> str:
    """Four of the family's content words plus four words of its own."""
    words = FAMILIES[family][0].split()
    return " ".join(rng.sample(words, 4) + [f"w{rng.randrange(400)}" for _ in range(4)])


def corpus(rng: random.Random, per: int = 8, *, linked: bool = True) -> tuple[dict[str, str], dict[str, float]]:
    texts = {f"{f}-{i}": paraphrase(rng, f) for f in FAMILIES for i in range(per)}
    effects = {m: FAMILIES[m.split("-")[0]][1] for m in texts}
    if not linked:
        values = list(effects.values())
        rng.shuffle(values)
        effects = dict(zip(effects, values))
    return texts, effects


def simulate(rng: random.Random, effects: dict[str, float], occasions: int = 1800, shown: int = 1) -> list[Row]:
    """Each occasion offers `shown` random memories, each independently withheld half the time."""
    lessons = sorted(effects)
    rows = []
    for i in range(occasions):
        offered = rng.sample(lessons, shown)
        injected = {m: rng.random() >= 0.5 for m in offered}
        p = 0.5 + sum(effects[m] for m in offered if injected[m])
        succeeded = rng.random() < min(0.95, max(0.05, p))
        rows += [Row(m, f"o{i}", injected[m], succeeded, 0.5, "", f"{i:08d}") for m in offered]
    return rows


def rmse(model: causal_gate.Model, effects: dict[str, float]) -> float:
    return (sum((model.lessons[m].mean - e) ** 2 for m, e in effects.items()) / len(effects)) ** 0.5


# -- kernel ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("powers", [(), (0,), (-1,), (0.5,), (True,)])
def test_fit_rejects_empty_or_invalid_kernel_power_grids(powers):
    with pytest.raises(ValueError, match="kernel powers"):
        effect_field.fit({}, {}, 0.04, effect_field.Kernel.lexical({}), powers=powers)


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_validation_threshold_requires_a_positive_integer_power_count(count):
    with pytest.raises(ValueError, match="power count"):
        effect_field.validate_z(count)


def test_tokens_keep_content_words_and_cjk_bigrams():
    assert effect_field.tokens("The 429 retry is in THE queue") == ["retry", "queue"]
    assert effect_field.tokens("退款流程") == ["退款", "款流", "流程"]


def test_lexical_similarity_is_cosine_on_tfidf_and_neighbors_exclude_the_query():
    kernel = effect_field.Kernel.lexical(
        {"a": "refund ledger invoice", "b": "refund ledger invoice", "c": "refund ledger", "d": "kubernetes helm"}
    )
    assert kernel.similarity("a", "b") == pytest.approx(1.0)
    assert kernel.similarity("a", "d") == 0.0
    assert 0.0 < kernel.similarity("a", "c") < 1.0
    near = kernel.neighbors(["a"], ["a", "b", "c", "d"])["a"]
    assert [m for m, _s in near] == ["b", "c"]
    assert kernel.similarity("a", "missing") == 0.0


def test_dense_kernels_normalize_and_the_digest_commits_to_the_inputs():
    kernel = effect_field.Kernel.from_embeddings(
        {"a": [3.0, 4.0], "b": [6.0, 8.0], "c": [-4.0, 3.0]}, kind="embed:test"
    )
    assert kernel.similarity("a", "b") == pytest.approx(1.0)
    assert kernel.similarity("a", "c") == pytest.approx(0.0, abs=1e-12)
    same = effect_field.Kernel.from_embeddings({"a": [3.0, 4.0], "b": [6.0, 8.0], "c": [-4.0, 3.0]}, kind="embed:test")
    other = effect_field.Kernel.from_embeddings({"a": [3.0, 4.0], "b": [6.0, 8.0], "c": [4.0, 3.0]}, kind="embed:test")
    assert kernel.digest() == same.digest() != other.digest()


def test_neighbors_match_pairwise_similarity_with_signed_embeddings_and_mixed_widths(monkeypatch):
    bag = effect_field.term_bag("refund ledger invoice")
    kernel = effect_field.Kernel.combined(
        {"q": bag, "opposite": bag},
        {
            "q": ("model", [1.0, 0.0]),
            "opposite": ("model", [-1.0, 0.0]),
            "wide": ("model", [1.0, 0.0, 0.0]),
            "peer": ("model", [2.0, 0.0, 0.0]),
        },
    )
    queries = ["q", "q", "wide"]
    pool = ["opposite", "peer"]
    expected = {
        q: tuple((m, round(kernel.similarity(q, m), 12)) for m in pool if kernel.similarity(q, m) > 1e-9)
        for q in queries
    }
    accelerated = kernel.neighbors(queries, pool)
    original = effect_field.importlib.import_module

    def without_numpy(name):
        if name == "numpy":
            raise ImportError("core installation")
        return original(name)

    monkeypatch.setattr(effect_field.importlib, "import_module", without_numpy)
    assert accelerated == kernel.neighbors(queries, pool) == expected


def test_dense_normalization_handles_extreme_finite_magnitudes():
    kernel = effect_field.Kernel.from_embeddings({"a": [1e308, 1e308], "b": [1e-300, 1e-300]})
    assert kernel.has("a") and kernel.has("b")
    assert kernel.similarity("a", "b") == pytest.approx(1.0)


def test_dense_kernel_accepts_native_encoder_float32_vectors():
    np = pytest.importorskip("numpy")
    kernel = effect_field.Kernel.from_embeddings({"a": np.asarray([3, 4], dtype=np.float32), "b": [6.0, 8.0]})
    assert kernel.similarity("a", "b") == pytest.approx(1.0)


@pytest.mark.parametrize("seed", range(4))
def test_batched_fit_preserves_scalar_grid_witnesses_and_priors(seed, monkeypatch):
    pytest.importorskip("numpy")
    rng = random.Random(seed)
    texts, effects = corpus(rng, per=16)
    kernel = effect_field.Kernel.lexical(texts)
    estimates = {m: (e + rng.uniform(-0.01, 0.01), rng.uniform(0.0001, 0.002)) for m, e in effects.items()}
    bases = {m: (0.0, 0.04) for m in texts}
    accelerated = effect_field.fit(estimates, bases, 0.04, kernel)

    def scalar_engine(hoods, scales, residual, noise):
        return lambda power, share: {
            q: effect_field._transfer(h, power, share, scales[q], scales, residual, noise) for q, h in hoods.items()
        }

    monkeypatch.setattr(effect_field, "_transfer_engine", scalar_engine)
    monkeypatch.setattr(effect_field, "_kernel_gram", lambda *_: None)
    reference = effect_field.fit(estimates, bases, 0.04, kernel)
    assert (accelerated.verdict, accelerated.power, accelerated.share) == (
        reference.verdict,
        reference.power,
        reference.share,
    )
    assert accelerated.lpd_gain == pytest.approx(reference.lpd_gain, abs=1e-10)
    assert accelerated.z == pytest.approx(reference.z, abs=1e-8)
    for m in texts:
        assert accelerated.witnesses(m) == reference.witnesses(m)
        assert accelerated.prior(m, *bases[m]) == pytest.approx(reference.prior(m, *bases[m]), abs=1e-10)


def test_native_factorization_failure_uses_the_scalar_reference_per_query(monkeypatch):
    np = pytest.importorskip("numpy")
    hood = effect_field._Neighborhood(("w",), (0.8,), ((1.0,),))
    hoods = {f"q{i}": hood for i in range(40)}
    scales = {m: 0.2 for m in [*hoods, "w"]}
    engine = effect_field._transfer_engine(hoods, scales, {"w": 0.3}, {"w": 0.001})

    def fail(_matrix):
        raise np.linalg.LinAlgError("simulated backend failure")

    monkeypatch.setattr(np.linalg, "cholesky", fail)
    expected = effect_field._transfer(hood, 2, 0.8, 0.2, scales, {"w": 0.3}, {"w": 0.001})
    assert engine(2, 0.8) == {m: expected for m in hoods}


def test_insufficient_evidence_does_not_build_a_native_gram_matrix(monkeypatch):
    texts = {f"m{i}": "refund ledger invoice" for i in range(80)}
    kernel = effect_field.Kernel.lexical(texts)

    def unexpected(*_args):
        raise AssertionError("insufficient evidence must not initialize the accelerator")

    monkeypatch.setattr(effect_field, "_kernel_gram", unexpected)
    field = effect_field.fit({"m0": (0.2, 0.001)}, {m: (0.0, 0.04) for m in texts}, 0.04, kernel, candidates=texts)
    assert field.verdict == effect_field.INSUFFICIENT


def test_dense_neighbor_batches_preserve_exact_topk_across_both_boundaries():
    rng = random.Random(71)
    queries = [f"q{i}" for i in range(129)]
    members = [f"m{i}" for i in range(513)]
    kernel = effect_field.Kernel.from_embeddings(
        {m: [rng.uniform(-1, 1) for _ in range(8)] for m in [*queries, *members]}
    )
    actual = kernel.neighbors(queries, members, 3)
    for q in queries:
        expected = sorted(((m, kernel.similarity(q, m)) for m in members), key=lambda item: (-item[1], item[0]))[:3]
        assert actual[q] == tuple((m, round(s, 12)) for m, s in expected)


@pytest.mark.parametrize("vector", [[float("nan")], [float("inf")], [10**1000], [True]])
def test_dense_kernel_rejects_invalid_values(vector):
    with pytest.raises(ValueError, match="finite"):
        effect_field.Kernel.from_embeddings({"a": vector})


# -- transfer --------------------------------------------------------------------------------------


def test_near_duplicate_witnesses_count_as_about_one_and_never_exceed_the_field_variance():
    tau2, share = 0.01, 0.95
    one = effect_field._Neighborhood(("a",), (1.0,), ((1.0,),))
    ids = tuple(f"a{i}" for i in range(10))
    ten = effect_field._Neighborhood(ids, (1.0,) * 10, tuple((1.0,) * 10 for _ in ids))
    residual = {m: 0.1 for m in ("a", *ids)}
    noise = {m: 1e-4 for m in ("a", *ids)}
    sd = {m: tau2**0.5 for m in residual}
    shift1, red1 = effect_field._transfer(one, 1, share, tau2**0.5, sd, residual, noise)
    shift10, red10 = effect_field._transfer(ten, 1, share, tau2**0.5, sd, residual, noise)
    assert red1 < red10 <= share * tau2
    assert red10 < 1.1 * red1  # independent copies would have cut the variance ten times
    assert 0.0 < shift1 < shift10 < 0.1


def test_the_field_validates_when_effects_follow_content_and_cuts_estimation_error():
    for seed in range(4):
        rng = random.Random(seed)
        texts, effects = corpus(rng)
        rows = simulate(rng, effects)
        model = causal_gate.fit(rows, kernel=effect_field.Kernel.lexical(texts))
        assert model.transfer is not None and model.transfer.validated, model.transfer.summary()
        assert model.transfer.z is not None and model.transfer.z >= effect_field.VALIDATE_Z
        assert rmse(model, effects) < rmse(causal_gate.fit(rows), effects)


def test_the_field_refuses_to_transfer_when_content_does_not_predict_effects():
    for seed in range(6):
        rng = random.Random(100 + seed)
        texts, effects = corpus(rng, linked=False)
        rows = simulate(rng, effects)
        model = causal_gate.fit(rows, kernel=effect_field.Kernel.lexical(texts))
        assert model.transfer is not None and model.transfer.verdict == effect_field.NOT_VALIDATED
        plain = causal_gate.fit(rows)
        assert {m: p.mean for m, p in model.lessons.items()} == {m: p.mean for m, p in plain.lessons.items()}
        assert causal_gate.rates(model) == causal_gate.rates(plain)


def test_too_few_measured_memories_are_insufficient_and_no_kernel_is_the_legacy_gate():
    rng = random.Random(7)
    texts, effects = corpus(rng, per=2)
    rows = simulate(rng, effects, occasions=200)
    model = causal_gate.fit(rows, kernel=effect_field.Kernel.lexical(texts), candidates=["refund-new"])
    assert model.transfer is not None and model.transfer.verdict == effect_field.INSUFFICIENT
    assert model.forecasts == {}
    legacy = causal_gate.fit(rows).to_dict()
    assert "field" not in legacy and "forecasts" not in legacy
    assert {k: v for k, v in model.to_dict().items() if k not in ("field", "forecasts")} == legacy


def test_a_new_paraphrase_starts_from_its_family_evidence_and_an_unrelated_memory_does_not():
    rng = random.Random(3)
    texts, effects = corpus(rng)
    rows = simulate(rng, effects)
    new = {"legacy-new": paraphrase(rng, "legacy"), "refund-new": paraphrase(rng, "refund"), "alien": "zebra quartz"}
    model = causal_gate.fit(rows, kernel=effect_field.Kernel.lexical({**texts, **new}), candidates=new)
    assert set(model.forecasts) == {"legacy-new", "refund-new"}
    assert not set(model.forecasts) & set(model.lessons)
    lesson_rates, _ = causal_gate.rates(model)
    assert lesson_rates["legacy-new"] > 0.7 and lesson_rates["refund-new"] < 0.3
    assert "alien" not in lesson_rates
    legacy = model.forecasts["legacy-new"]
    assert legacy.n_injected == legacy.n_withheld == 0 and legacy.raw_effect is None
    assert model.posterior("legacy-new") == legacy


def test_shared_occasion_outcomes_cannot_validate_transfer_by_memory_count():
    rng = random.Random(0)
    texts, effects = corpus(rng)
    rows = simulate(rng, effects, occasions=600, shown=3)
    model = causal_gate.fit(rows, kernel=effect_field.Kernel.lexical(texts), candidates=["new"])
    assert not model.transfer.validated
    assert model.transfer.validation_units < effect_field.MIN_MEASURED
    assert model.forecasts == {}


def test_duplicating_content_families_cannot_manufacture_independent_evidence():
    vectors = {f"family-{family}-copy-{copy}": [1.0, family / 10] for family in range(6) for copy in range(8)}
    kernel = effect_field.Kernel.from_embeddings(vectors)
    estimates = {m: (0.2, 0.001) for m in vectors}
    bases = {m: (0.0, 0.04) for m in vectors}
    fitted = effect_field.fit(estimates, bases, 0.04, kernel)
    assert not fitted.validated
    assert fitted.validation_units == 6


def test_constant_positive_predictive_gains_are_not_infinite_evidence():
    vectors = {str(i): [1.0] + [float(i == j) for j in range(12)] for i in range(12)}
    fitted = effect_field.fit(
        {m: (0.3, 0.001) for m in vectors},
        {m: (0.0, 0.04) for m in vectors},
        0.04,
        effect_field.Kernel.from_embeddings(vectors),
    )
    assert fitted.z == 0.0 and not fitted.validated
    assert fitted.lpd_gain > 0 and fitted.validation_units == 12


def test_lookalikes_find_opposing_pairs_beyond_the_display_neighbor_cap():
    vectors = {**{f"positive-{i}": [1.0, 0.0] for i in range(14)}, **{f"negative-{i}": [1.0, 0.1] for i in range(14)}}
    kernel = effect_field.Kernel.from_embeddings(vectors)
    estimates = {m: (0.3 if m.startswith("positive") else -0.3, 0.0001) for m in vectors}
    assert all(
        all(b.split("-")[0] == a.split("-")[0] for b, _ in near)
        for a, near in kernel.neighbors(vectors, vectors).items()
    )
    pairs = effect_field.lookalikes(estimates, kernel)
    assert len(pairs) == 14 * 14
    assert all(estimates[p.a][0] * estimates[p.b][0] < 0 for p in pairs)


def test_validation_neighbor_filtering_matches_exact_family_exclusion():
    texts = {str(i): f"shared content family{i // 4}" for i in range(40)}
    kernel = effect_field.Kernel.lexical(texts)
    groups = {str(i): str(i // 4) for i in range(40)}
    ids = sorted(texts)
    expected = {
        q: tuple(
            sorted(
                ((m, round(kernel.similarity(q, m), 12)) for m in ids if groups[q] != groups[m]),
                key=lambda item: (-item[1], item[0]),
            )[:12]
        )
        for q in ids
    }
    assert kernel.neighbors(ids, ids, families=groups) == expected
    gram = effect_field._kernel_gram(kernel, ids)
    if gram is not None:
        assert gram.neighbors(kernel, ids, ids, 12, families=groups) == expected


def test_missing_embedding_spaces_dilute_the_concatenated_kernel():
    kernel = effect_field.Kernel.combined(
        {"both": {"term": 1}}, {"both": ("encoder", [1, 0]), "dense-only": ("encoder", [1, 0])}
    )
    assert kernel.similarity("both", "dense-only") == pytest.approx(2**-0.5)


def test_lookalikes_with_opposite_effects_are_flagged_and_agreeing_ones_are_not():
    kernel = effect_field.Kernel.lexical(
        {
            "use-v1": "call the refunds endpoint for every refund",
            "use-v2": "call the refunds endpoint for every refund now",
            "a": "rotate keys quarterly",
            "b": "rotate keys quarterly always",
        }
    )
    estimates = {"use-v1": (-0.2, 0.001), "use-v2": (0.2, 0.001), "a": (0.1, 0.001), "b": (0.12, 0.001)}
    [pair] = effect_field.lookalikes(estimates, kernel)
    assert (pair.a, pair.b) == ("use-v1", "use-v2") and pair.q_value < 0.05
    assert effect_field.lookalikes({**estimates, "use-v2": (0.2, 1.0)}, kernel) == []


# -- the store -------------------------------------------------------------------------------------


def _write_lesson(root: str, slug: str, text: str, status: str = "active") -> None:
    from unimem import lesson_io, paths

    path = os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = {"name": slug, "description": text, "status": status, "importance": 3, "tags": []}
    lesson_io.write_lesson(path, fm, text, root=root, actor="test", reason="fixture")


def _run(root: str, clock, rng: random.Random, effects: dict[str, float], start: int, n: int) -> None:
    lessons = sorted(effects)
    # Independent occasions establish transfer; pooled/shared-outcome refusal
    # is exercised separately. Retain the original number of memory exposures.
    for i in range(3 * start, 3 * (start + n)):
        offered = rng.sample(lessons, 1)
        withheld = holdout_io.assign_and_log(root, offered, occasion_id=f"T-{i}", rate=0.5, salt="pinned")
        p = 0.5 + sum(effects[m] for m in offered if m not in withheld)
        holdout_io.record_outcome(root, f"T-{i}", rng.random() < min(0.95, max(0.05, p)))
        clock.now += datetime.timedelta(seconds=30)


def test_thompson_field_schedules_forecast_new_lessons_and_the_run_still_audits(tmp_path, clock, monkeypatch):
    root = str(tmp_path)
    rng = random.Random(21)
    texts, effects = corpus(rng)
    for slug, text in texts.items():
        _write_lesson(root, slug, text)
    holdout_io.configure(root, rate=0.5, salt="pinned")
    first = allocation.enable(root, allocation.Policy(era_occasions=200, mode=allocation.MODE_FIELD))
    clock.now += datetime.timedelta(seconds=10)
    _run(root, clock, rng, effects, 0, 600)

    _write_lesson(root, "legacy-new", paraphrase(rng, "legacy"))
    _write_lesson(root, "refund-new", paraphrase(rng, "refund"))
    _write_lesson(root, "retired", paraphrase(rng, "legacy"), status="retired")
    second = allocation.plan(root)
    assert second.previous == first.digest and second.policy["mode"] == "thompson-field"
    assert second.basis["mode"] == "thompson-field" and second.basis["field"]["verdict"] == "VALIDATED"
    assert second.basis["field"]["kernel_digest"].startswith("sha256:")
    assert set(second.basis["forecasts"]) == {"legacy-new", "refund-new"}
    assert second.rates["legacy-new"] > 0.7 and second.rates["refund-new"] < 0.3
    assert "retired" not in second.rates

    clock.now += datetime.timedelta(seconds=10)
    effects.update({"legacy-new": -0.2, "refund-new": 0.2})
    _run(root, clock, rng, effects, 600, 200)
    later = [r for r in holdout_io.read_log(root)[0] if r.at >= second.effective_at()]
    assert later and all(r.rate == second.rate_for(r.lesson) for r in later)
    assert allocation.verify(root) == []
    rows, _rate, _ = experiment_cmd._load(root)
    report = integrity.audit(rows, schedules=allocation.history(root))
    assert next(f for f in report.findings if f.check == "assignment_drift").severity != (
        integrity.SEVERITY_INVALIDATES
    )

    assert allocation.field_mode(root) and not allocation.field_mode(root, "another-experiment")
    evaluated = []
    evaluate = causal_gate.evaluate
    monkeypatch.setattr(causal_gate, "evaluate", lambda *a, **kw: evaluated.append(kw) or evaluate(*a, **kw))
    out = causal_gate.report(root)
    section = out["effect_field"]
    assert section["used_by_gate"] is True and section["verdict"] == "VALIDATED"
    assert "field" in out["model"] and section["off_policy"]["eval_occasions"] > 0
    assert out["off_policy"] == section["off_policy"] and len(evaluated) == 1  # the field gate is scored once
    plain = causal_gate.report(root, field=False)
    assert plain["effect_field"]["used_by_gate"] is False and len(evaluated) == 3
    assert [call.get("kernel") is None for call in evaluated] == [False, False, True]


def test_an_unbounded_z_is_reported_as_strict_json_and_rendered():
    field = effect_field.Field(effect_field.VALIDATED, "lexical", measured=12, lpd_gain=0.2, z=float("inf"))
    summary = field.summary()
    assert summary["z"] is None and json.loads(json.dumps(summary, allow_nan=False)) == summary
    [line, *_] = causal_gate._render_field({**summary, "used_by_gate": True})
    assert "z unbounded" in line and "VALIDATED" in line
    finite = effect_field.Field(effect_field.NOT_VALIDATED, "lexical", measured=12, lpd_gain=-0.1, z=-1.234567)
    assert finite.summary()["z"] == -1.2346
    assert "z -1.23" in causal_gate._render_field({**finite.summary(), "used_by_gate": False})[0]


def test_legacy_thompson_policies_hash_exactly_as_before():
    assert asdict(allocation.Policy(mode="thompson")) == {
        "explore": 0.5,
        "monitor": 0.05,
        "era_occasions": 250,
        "mode": "thompson",
    }
    with pytest.raises(ValueError, match="monitor"):
        allocation.Policy(explore=0.8, monitor=0.6, mode=allocation.MODE_FIELD)


def test_the_gate_command_reports_the_field_and_enables_field_mode(tmp_path, clock, capsys):
    root = str(tmp_path)
    rng = random.Random(5)
    texts, effects = corpus(rng)
    for slug, text in texts.items():
        _write_lesson(root, slug, text)
    holdout_io.configure(root, rate=0.5, salt="pinned")
    _run(root, clock, rng, effects, 0, 600)
    _write_lesson(root, "legacy-new", paraphrase(rng, "legacy"))
    assert main(["allocate", "gate", "--dest", root]) == 0
    text = capsys.readouterr().out
    assert "effect field (lexical-tfidf-v2): VALIDATED" in text and "diagnostic only" in text
    assert "forecast legacy-new" in text
    assert main(["allocate", "gate", "--json", "--dest", root]) == 0
    doc = json.loads(capsys.readouterr().out)
    [forecast] = doc["effect_field"]["forecasts"]
    # A validated field must forecast in the measured harmful direction; its
    # magnitude depends on the independent validation sample, not a fixed rate.
    assert forecast["lesson"] == "legacy-new" and forecast["withhold"] > 0.5 and forecast["witnesses"]
    assert main(["allocate", "enable", "--mode", "thompson-field", "--dest", root]) == 0
    assert "unlisted memories: 50% withheld" in capsys.readouterr().out


# -- context field: a robot fleet's embodiments, sites and skills ----------------------------------

EMBODIMENT_EFFECT = {"jaw": 0.25, "suction": -0.25, "dexterous": 0.0}
SITES = ("depot", "yard", "lab", "dock")


def test_context_similarity_is_the_share_of_matching_components_within_one_memory():
    kernel = effect_field.CellKernel.build(
        None,
        {
            "a": ("m", "ur5/depot/grasp"),
            "b": ("m", "ur5/yard/grasp"),
            "c": ("m", "franka/depot/pour"),
            "d": ("m", "code"),
            "e": ("m", "support"),
            "f": ("other", "ur5/depot/grasp"),
        },
    )
    assert kernel.kind == "identity*context-components-v1"
    assert kernel.similarity("a", "b") == pytest.approx(2 / 3)
    assert kernel.similarity("a", "c") == pytest.approx(1 / 3)
    assert kernel.similarity("d", "e") == 0.0  # short labels are padded with pads that match nothing
    assert kernel.similarity("a", "f") == 0.0  # without a content kernel, different memories never compare
    assert kernel.similarity("a", "a") == pytest.approx(1.0)


@pytest.mark.parametrize("context", ["alien/moon/weld", "code", "other/site/task/long"])
def test_a_validated_context_field_cannot_transfer_into_an_orthogonal_context(context):
    cells = {f"{e}-{s}": ("memory", f"{e}/{s}/pick") for e in EMBODIMENT_EFFECT for s in SITES}
    estimates = {name: (EMBODIMENT_EFFECT[c.split("/")[0]], 0.001) for name, (_, c) in cells.items()}
    cells["new"] = ("memory", context)
    bases = {name: (0.0, 0.1) for name in cells}
    kernel = effect_field.CellKernel.build(None, cells)
    model = effect_field.fit(estimates, bases, 0.1, kernel, candidates=["new"], powers=(1,))
    assert model.validated, model.summary()
    assert model.witnesses("new") == ()
    assert not model.informs("new")
    assert model.prior("new", *bases["new"]) == bases["new"]


@pytest.mark.parametrize("k", [-1, True, 1.5, "2"])
def test_context_neighbors_validate_the_same_count_contract_as_content_neighbors(k):
    kernel = effect_field.CellKernel.build(None, {"a": ("m", "robot/site"), "b": ("m", "robot/other")})
    with pytest.raises(ValueError, match="neighbor count"):
        kernel.neighbors(["a"], ["a", "b"], k=k)


def test_zero_context_neighbors_does_not_build_a_pool(monkeypatch):
    kernel = effect_field.CellKernel.build(None, {"a": ("m", "robot/site")})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("zero neighbors must not compute a content pool")

    monkeypatch.setattr(effect_field.CellKernel, "_pool", forbidden)
    assert kernel.neighbors(["a"], ["a"], k=0) == {"a": ()}


def test_context_neighbors_accept_unknown_queries_and_pool_members():
    kernel = effect_field.CellKernel.build(None, {"a": ("m", "robot/site"), "b": ("m", "robot/other")})
    assert kernel.similarity("unknown", "a") == 0.0
    assert kernel.neighbors(["unknown", "a"], ["absent", "a", "b"]) == {
        "unknown": (),
        "a": (("b", 0.5),),
    }


def test_conflicting_text_benchmark_has_opposing_copies_and_refuses_shared_outcome_evidence(monkeypatch):
    from benchmarks import effect_field_bench as bench

    batches = bench._arrivals(random.Random(0), False, conflict=True)
    for batch in batches:
        for index in range(0, len(batch), 2):
            assert batch[index][1] == batch[index + 1][1]
            assert batch[index][2] == -batch[index + 1][2]
    monkeypatch.setattr(bench, "ERAS", 2)
    monkeypatch.setattr(bench, "ERA_OCCASIONS", 100)
    result = bench.run(0, "thompson-field", False, conflict=True)
    assert result["validated_eras"] == 0


def test_context_neighbors_preserve_pairwise_order_across_a_large_cell_pool():
    cells = {str(i): ("m", f"robot/{i % 7}/pick") for i in range(1000)}
    kernel = effect_field.CellKernel.build(None, cells)
    for k in (1, 12, 1001):
        actual = kernel.neighbors(["0", "1"], cells, k=k)
        for query in actual:
            expected = sorted(
                ((cell, round(kernel.similarity(query, cell), 12)) for cell in cells if cell != query),
                key=lambda item: (-item[1], item[0]),
            )[:k]
            assert actual[query] == tuple(expected)


def _fleet(seed: int, *, linked: bool, per: int = 30) -> tuple[list[Row], dict[str, float]]:
    rng = random.Random(seed)
    contexts = [f"{e}/{s}/pick" for e in EMBODIMENT_EFFECT for s in SITES]
    truth = {c: EMBODIMENT_EFFECT[c.split("/")[0]] for c in contexts}
    if not linked:
        values = list(truth.values())
        rng.shuffle(values)
        truth = dict(zip(contexts, values))
    rows, i = [], 0
    for c in contexts:
        for _ in range(per):
            shown = rng.random() >= 0.5
            rows.append(Row("gripper-tip", f"o{i}", shown, rng.random() < 0.5 + (truth[c] if shown else 0), 0.5, c))
            filler = rng.random() >= 0.5
            rows.append(Row("filler", f"o{i}", filler, rng.random() < 0.5, 0.5, c))
            i += 1
    return rows, truth


def _context_rmse(model: causal_gate.Model, truth: dict[str, float]) -> float:
    errors = [(model.contexts[("gripper-tip", c)].mean - t) ** 2 for c, t in truth.items()]
    return (sum(errors) / len(errors)) ** 0.5


def test_sparse_robot_contexts_borrow_from_contexts_sharing_an_embodiment():
    for seed in (0, 1, 4):
        rows, truth = _fleet(seed, linked=True, per=60)
        model = causal_gate.fit(rows, context_transfer=True)
        assert model.context_transfer["gripper-tip"].validated, model.context_transfer["gripper-tip"].summary()
        assert _context_rmse(model, truth) < _context_rmse(causal_gate.fit(rows), truth)
        summary = model.to_dict()["context_field"]
        assert "gripper-tip" in summary["validated"] and summary["kernel"] == "identity*context-components-v1"


def test_cross_fitted_context_validation_refuses_a_marginal_sparse_gain():
    rows, _ = _fleet(4, linked=True, per=30)
    model = causal_gate.fit(rows, context_transfer=True)
    field = model.context_transfer["gripper-tip"]
    assert field.lpd_gain > 0 and field.z < field.threshold
    assert not field.validated
    assert model.contexts == causal_gate.fit(rows).contexts


def test_a_contexts_outcomes_cannot_change_its_cross_fitted_nuisance_fit(monkeypatch):
    from dataclasses import replace

    rows, _ = _fleet(0, linked=True, per=60)
    target = "gripper-tip\x1ejaw/depot/pick"
    captured = []
    original = effect_field.fit

    def capture(*args, **kwargs):
        cases = kwargs.get("validation_cases", {})
        if target in cases:
            captured.append(cases[target])
        return original(*args, **kwargs)

    monkeypatch.setattr(effect_field, "fit", capture)
    causal_gate.fit(rows, context_transfer=True)
    occasions = {row.occasion_id for row in rows if row.context == "jaw/depot/pick"}
    changed = [replace(row, succeeded=not row.succeeded) if row.occasion_id in occasions else row for row in rows]
    causal_gate.fit(changed, context_transfer=True)
    assert len(captured) == 2 and captured[0] == captured[1]


def test_the_context_field_stays_out_when_components_do_not_predict_effects():
    for seed in range(5):
        rows, _truth = _fleet(seed, linked=False)
        model = causal_gate.fit(rows, context_transfer=True)
        assert not model.context_transfer["gripper-tip"].validated
        plain = causal_gate.fit(rows)
        assert model.contexts[("gripper-tip", "jaw/depot/pick")] == plain.contexts[("gripper-tip", "jaw/depot/pick")]


@pytest.mark.parametrize("per", [12, 60])
def test_content_similar_memories_lend_each_other_context_evidence_only_when_it_validates(per):
    rng = random.Random(9)
    contexts = [f"{e}/{s}/pick" for e in EMBODIMENT_EFFECT for s in SITES]
    rows, i = [], 0
    for memory in ("regrasp-a", "regrasp-b"):
        for c in contexts:
            for _ in range(per):
                shown = rng.random() >= 0.5
                effect = EMBODIMENT_EFFECT[c.split("/")[0]] if shown else 0.0
                rows.append(Row(memory, f"o{i}", shown, rng.random() < 0.5 + effect, 0.5, c))
                i += 1
    siblings = effect_field.Kernel.from_embeddings({"regrasp-a": [1.0, 0.1], "regrasp-b": [1.0, 0.12]}, "enc")
    alone = causal_gate.fit(rows, context_transfer=True)
    together = causal_gate.fit(rows, kernel=siblings, context_transfer=True)
    truth = {c: EMBODIMENT_EFFECT[c.split("/")[0]] for c in contexts}

    def rmse(model):
        errors = [(model.contexts[("regrasp-a", c)].mean - t) ** 2 for c, t in truth.items()]
        return (sum(errors) / len(errors)) ** 0.5

    if per == 12:
        assert not together.context_transfer["regrasp-a"].validated
        assert together.contexts == alone.contexts
        return
    assert together.context_transfer["regrasp-a"].validated
    witnesses = together.context_transfer["regrasp-a"].witnesses("regrasp-a\x1ejaw/depot/pick")
    assert any(w.startswith("regrasp-b\x1e") for w, _s in witnesses)
    assert rmse(together) < rmse(alone)


def test_context_neighbors_expand_past_unrelated_content_shortlist():
    content = effect_field.Kernel.from_embeddings(
        {"query": [1.0, 0.0], **{f"memory-{i:02}": [1.0, i / 100] for i in range(1, 15)}}
    )
    cells = {
        "query": ("query", "robot/site"),
        **{f"cell-{i:02}": (f"memory-{i:02}", "unrelated/place" if i < 14 else "robot/site") for i in range(1, 15)},
    }
    kernel = effect_field.CellKernel.build(content, cells)
    assert kernel.neighbors(["query"], cells, k=1)["query"] == (
        ("cell-14", round(kernel.similarity("query", "cell-14"), 12)),
    )
    # Reusing the original bounded pool cache must retain the same answer.
    assert kernel.neighbors(["query"], cells, k=1)["query"][0][0] == "cell-14"


def test_context_neighbors_honor_counts_larger_than_content_shortlist():
    cells = {f"cell-{i:02}": (f"memory-{i:02}", "robot/site") for i in range(30)}
    content = effect_field.Kernel.from_embeddings({memory: [1.0] for memory, _context in cells.values()})
    kernel = effect_field.CellKernel.build(content, cells)
    for count in (20, 5, 30):
        actual = kernel.neighbors(["cell-00"], cells, k=count)["cell-00"]
        expected = tuple((cell, 1.0) for cell in sorted(cells) if cell != "cell-00")[:count]
        assert actual == expected


def test_context_neighbors_include_later_content_ties_in_cell_id_order():
    content = effect_field.Kernel.from_embeddings(
        {"query": [1.0, 0.0], **{f"memory-{i:02}": [1.0, 0.0] for i in range(13)}}
    )
    cells = {
        "query": ("query", "robot/site"),
        **{f"z-cell-{i:02}": (f"memory-{i:02}", "robot/site") for i in range(12)},
        "a-cell": ("memory-12", "robot/site"),
    }
    kernel = effect_field.CellKernel.build(content, cells)
    assert kernel.neighbors(["query"], cells, k=1)["query"] == (("a-cell", 1.0),)


def test_context_pair_caches_stay_bounded_without_changing_similarity_or_neighbors(monkeypatch):
    monkeypatch.setattr(effect_field, "MAX_SIMILARITY_CACHE", 3)
    content = effect_field.Kernel.from_embeddings({f"memory-{i:02}": [1.0] for i in range(20)})
    cells = {f"cell-{19 - i:02}": (f"memory-{i:02}", f"robot/site-{i}") for i in range(20)}
    kernel = effect_field.CellKernel.build(content, cells)
    for a in cells:
        for b in cells:
            assert kernel.similarity(a, b) == (1.0 if a == b else 0.5)
    assert len(kernel._shared) == len(kernel._sims) == 3
    context_cache, content_cache = dict(kernel._shared), dict(kernel._sims)
    actual = kernel.neighbors(["cell-19"], cells, k=5)["cell-19"]
    assert actual == tuple((cell, 0.5) for cell in sorted(cells) if cell != "cell-19")[:5]
    assert kernel._shared == context_cache and kernel._sims == content_cache

    # Saturation preserves ordinary cache hits while computing new pairs on demand.
    def forbidden(*_args):
        raise AssertionError("a retained content similarity must remain cached")

    monkeypatch.setattr(effect_field.Kernel, "similarity", forbidden)
    retained_pair = next(iter(content_cache))
    assert kernel._memory(*retained_pair) == content_cache[retained_pair]
