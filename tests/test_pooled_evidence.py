"""UniMem model coverage, unbiased scores, private releases and trusted pooling."""
from __future__ import annotations

import copy
import math
import random

import pytest

from unimem import frontier_protocol as fp
from unimem import pooled_evidence as pool

UNIT = fp.digest("public skill body")


def aggregate(tenant, mean, n=200, release="r1"):
    return {"type": pool.DOMAIN, "version": 1, "tenant": tenant, "unit_hash": UNIT,
        "release_id": release, "score_bound": 20.0, "noise_variance": 0.0, "nuisance": [.5, .5],
        "classification": "estimate", "privacy": None, "e_value": 1.0,
        "arms": {arm: {"count": n//2, "score_sum": mean*n/2, "square_sum": (1+mean*mean)*n/2}
                 for arm in pool.ARMS}}


def traffic(rng, n, effect=.2):
    rows = []
    for i in range(n):
        rate = .15 if i < n//2 else .85
        injected = rng.random() >= rate
        base = .25 if i < n//2 else .65
        rows.append({"occasion_id": str(i), "rate": rate, "injected": injected,
                     "outcome": rng.random() < base + effect*injected})
    return rows


def test_student_t_reference_quantiles_and_nonfinite():
    assert pool.t_critical(1) == pytest.approx(12.706204736, rel=1e-8)
    assert pool.t_critical(2) == pytest.approx(4.302652730, rel=1e-8)
    assert pool.t_critical(10) == pytest.approx(2.228138852, rel=1e-8)
    assert pool.t_critical(30) == pytest.approx(2.042272456, rel=1e-8)
    for alpha in (float("nan"), -1, 0):
        with pytest.raises(ValueError):
            pool.t_critical(2, alpha)


def test_seeded_heterogeneity_coverage_with_unbalanced_study_precisions():
    rng = random.Random(2901)
    covered, positive_variance = 0, 0
    # Twenty-four independent owners, with strongly unbalanced precisions.
    # Eight-owner DL/HK intervals can under-cover with this imbalance; see docs.
    counts = [100000, 50000, 2000, 400, 100, 40, 20, 20] * 3
    for _ in range(120):
        contributions = [aggregate(str(i), rng.gauss(.1, .25) + rng.gauss(0, 1/math.sqrt(n)), n)
                         for i, n in enumerate(counts)]
        result = pool.pool(contributions, groups={str(i): str(i) for i in range(len(counts))})
        covered += result["interval"][0] <= .1 <= result["interval"][1]
        positive_variance += result["tau_squared"] > .01
    assert covered >= 108  # 95% target, 5-point finite Monte Carlo tolerance.
    assert positive_variance >= 108


def test_frozen_nuisance_aipw_is_unbiased_with_unequal_propensities_and_time_trend():
    rng = random.Random(291)
    estimates = []
    for _ in range(100):
        body = pool.summarize("tenant", UNIT, traffic(rng, 400), release_id="r")
        estimates.append(sum(a["score_sum"] for a in body["arms"].values()) / 400)
    assert sum(estimates)/len(estimates) == pytest.approx(.2, abs=.025)


def test_sybil_shards_do_not_create_studies_or_change_the_effect():
    original = aggregate("one", .3, 400)
    second = aggregate("two", -.1, 200)
    before = pool.pool([original, second], groups={"one": "owner", "two": "other"})
    split = [aggregate("alias-a", .3, 200), aggregate("alias-b", .3, 200), second]
    after = pool.pool(split, groups={"alias-a": "owner", "alias-b": "owner", "two": "other"})
    assert after["effect"] == pytest.approx(before["effect"])
    assert after["interval"] == pytest.approx(before["interval"])
    assert after["ownership_groups"] == before["ownership_groups"] == 2
    assert len(after["group_power"]) == 2
    assert len(after["tenant_power"]) == 3
    with pytest.raises(PermissionError):
        pool.pool(split, groups={"two": "other"})


def test_e_value_combination_states_its_null_and_power_limits():
    a, b = aggregate("a", .1), aggregate("b", .2)
    a["e_value"], b["e_value"] = 2, 4
    mean = pool.pool([a, b], groups={"a": "a", "b": "b"})
    product = pool.pool([a, b], groups={"a": "a", "b": "b"}, independent=True)
    assert mean["combined_e_value"] == pytest.approx(3)
    assert product["combined_e_value"] == pytest.approx(8)
    assert mean["classification"] == "estimate" and mean["release_certificate"] is False
    assert mean["minimum_detectable_effect"] > 0
    assert len(mean["leave_one_out"]) == 2
    assert "conditional directional" in mean["e_value_null"]
    with pytest.raises(ValueError, match="replayed"):
        pool.pool([a, a], groups={"a": "a"})
    altered = {**b, "unit_hash": "0"*64}
    with pytest.raises(ValueError, match="fingerprint"):
        pool.pool([a, altered], groups={"a": "a", "b": "b"})


def test_gaussian_calibration_and_private_release_do_not_leak_exact_evidence(monkeypatch):
    scales = []
    monkeypatch.setattr(random.SystemRandom, "gauss", lambda self, mean, sigma: scales.append(sigma) or 0.0)
    body = pool.summarize("a", UNIT, traffic(random.Random(4), 40), release_id="private",
                          epsilon=.5, delta=1e-6)
    bound = body["score_bound"]
    sigma = math.hypot(2*bound, bound**2) * math.sqrt(2*math.log(1.25/1e-6)) / .5
    assert scales == pytest.approx([sigma]*4)
    assert body["noise_variance"] == pytest.approx(sigma*sigma)
    assert body["e_value"] == 1
    pool.validate(body)
    body["e_value"] = 2
    with pytest.raises(ValueError, match="raw e-value"):
        pool.validate(body)


@pytest.mark.parametrize("field,value", [("score_sum", float("nan")), ("square_sum", -1), ("count", True)])
def test_invalid_stats_rejected(field, value):
    body = aggregate("a", .2)
    body["arms"]["treated"][field] = value
    with pytest.raises(ValueError):
        pool.validate(body)


def test_signed_contributions_privacy_budget_and_scope(tmp_path):
    pytest.importorskip("cryptography")
    from unimem import memory_authority, pooled_io
    from unimem import nextwave_store as store

    receiver = str(tmp_path / "receiver")
    bundles = []
    for tenant, mean in (("a", .3), ("b", .2)):
        root = str(tmp_path / tenant)
        with memory_authority.writer(tenant, "operator"):
            card = pooled_io.issue_identity(root, tenant)
            pooled_io.register(receiver, tenant, principal=tenant, public_key=card["public_key"],
                group=tenant, stake_cents=100, expires_at="2099-01-01T00:00:00+00:00")
            bundle = pooled_io.contribute(root, tenant, UNIT, traffic(random.Random(1), 80, mean), release_id="r")
        pooled_io.accept(receiver, bundle)
        bundles.append(bundle)
    assert pooled_io.report(receiver, UNIT)["ownership_groups"] == 2
    forged = copy.deepcopy(bundles[0])
    forged["signed"]["record"]["contribution"]["tenant"] = "b"
    with pytest.raises(PermissionError):
        pooled_io.accept(receiver, forged)
    expired = copy.deepcopy(bundles[0])
    expired["signed"]["record"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(PermissionError):
        pooled_io.accept(receiver, expired)
    root = str(tmp_path / "private")
    pooled_io.configure_privacy(root, "a", epsilon=.5, delta=1e-5)
    rows = traffic(random.Random(2), 100)
    private = pooled_io.contribute(root, "a", UNIT, rows, release_id="p", epsilon=.5, delta=1e-6)
    assert pooled_io.contribute(root, "a", UNIT, rows, release_id="p", epsilon=.5, delta=1e-6) == private
    with pytest.raises(PermissionError, match="budget"):
        pooled_io.contribute(root, "a", UNIT, rows, release_id="p2", epsilon=.5, delta=1e-6)
    with pytest.raises(ValueError):
        pooled_io.configure_privacy(root, "a", epsilon=1, delta=1e-5)
    store.put(root, "test", "private", {"text": "scoped"}, scopes=["tenant:a"])
    with pytest.raises(PermissionError):
        store.get(root, "test", "private", context=["tenant:b"])
    assert store.get(root, "test", "private", context=["tenant:a"])["text"] == "scoped"
    store.erase(root, ["private"])
    with pytest.raises(ValueError):
        store.get(root, "test", "private", context=["tenant:a"])


def test_cli_and_mcp_pool_surfaces(tmp_path):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import call, cli
    from unimem.mcp_server import build_server

    request = tmp_path / "budget.json"
    request.write_text('{"tenant":"a","epsilon":0.5,"delta":0.000001}')
    result = cli("experiment", "pool", "privacy", str(request), "--dest", str(tmp_path))
    assert result.returncode == 0, result.stderr
    response = call(build_server(str(tmp_path)), "pooled_effect", unit_hash=UNIT)
    assert response["ok"] is False
