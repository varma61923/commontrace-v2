"""Independent rational arithmetic and tamper tests for complete effect witnesses."""
from __future__ import annotations

import json
from copy import deepcopy
from fractions import Fraction

import pytest

from unimem import frontier_protocol as fp
from unimem import zk_effect as zk


def witness(count=200, *, dp=False, kind="effect"):
    entries = [{"episode": "episode-"+str(index).zfill(5), "propensity": [50_000, 200_000, 500_000, 950_000][index % 4]}
               for index in range(count)]
    hiding, noise = "12"*32, "34"*32
    plan = zk.plan("study", entries, commitment_key=zk.seed_key(hiding), kind=kind,
        harm_limit_ppm=100_000 if kind == "harm" else None,
        dp={"numerator": 3, "denominator": 5, "noise_key": zk.seed_key(noise, noise=True)} if dp else None)
    challenge = zk.challenge(plan, "ab"*32)
    rows = []
    for index, entry in enumerate(zk.cohort(entries)):
        assigned = zk.assignment(plan, challenge, entry["episode"], entry["propensity"])
        outcome = 0 if kind == "harm" else (index*154_231+int(assigned)*330_271) % 1_000_001
        rows.append({**entry, "assigned": assigned, "outcome": outcome})
    return {"plan": plan, "challenge": challenge, "rows": rows, "commitment_seed": hiding,
            "noise_seed": noise if dp else None}


def test_transparent_verification_requires_the_complete_witness_and_an_external_challenge_pin():
    observed = witness()
    proof = zk.transparent(observed)
    assert not proof["private_rows"]
    assert "zero-knowledge" not in json.dumps(proof).lower()
    assert zk.verify_transparent(proof, observed, fp.digest(observed["challenge"]))
    assert not zk.verify_transparent(proof, observed, "f"*64)
    assert proof["statement"]["count"] == len(observed["rows"])
    assert proof["statement"]["execution_attested"] is False


@pytest.mark.parametrize("field", ["outcome", "assigned", "propensity", "episode", "count", "lower"])
def test_altered_rows_statements_or_cohort_counts_are_detected(field):
    observed = witness()
    proof = zk.transparent(observed)
    expected = fp.digest(observed["challenge"])
    if field == "outcome":
        observed["rows"][0]["outcome"] += 1
    elif field == "assigned":
        observed["rows"][0]["assigned"] = not observed["rows"][0]["assigned"]
    elif field == "propensity":
        observed["rows"][0]["propensity"] += 1
    elif field == "episode":
        observed["rows"].pop()
    elif field == "count":
        proof["statement"]["count"] -= 1
    else:
        proof["statement"]["lower_ppm"] += 1
    assert not zk.verify_transparent(proof, observed, expected)


def test_fixed_point_error_against_independent_exact_rational_ipw_and_hajek():
    observed = witness(700)
    report = zk.statement(observed)
    treated, control, wt, wc, total = Fraction(0), Fraction(0), Fraction(0), Fraction(0), Fraction(0)
    for row in observed["rows"]:
        y = Fraction(row["outcome"], zk.SCALE)
        p = Fraction(row["propensity"], zk.SCALE)
        if row["assigned"]:
            treated += y/p
            wt += 1/p
            total += y/p
        else:
            control += y/(1-p)
            wc += 1/(1-p)
            total -= y/(1-p)
    assert abs(Fraction(report["estimate_ppm"], zk.SCALE)-total/len(observed["rows"])) <= Fraction(2, zk.SCALE)
    exact_hajek = treated/wt-control/wc
    assert abs(Fraction(report["hajek_ppm"], zk.SCALE)-exact_hajek) <= Fraction(6, zk.SCALE)


def test_nonce_and_salted_outcome_commitments_are_binding_without_changing_the_cohort():
    original = witness()
    other = deepcopy(original)
    other["challenge"] = zk.challenge(other["plan"], "cd"*32)
    with pytest.raises(ValueError, match="assignment"):
        zk.statement(other)
    other = deepcopy(original)
    other["commitment_seed"] = "56"*32
    with pytest.raises(ValueError, match="seed"):
        zk.statement(other)
    other["plan"]["commitment_key"] = zk.seed_key(other["commitment_seed"])
    other["challenge"] = zk.challenge(other["plan"], "ab"*32)
    for row in other["rows"]:
        row["assigned"] = zk.assignment(other["plan"], other["challenge"], row["episode"], row["propensity"])
    assert zk.statement(original)["rows_root"] != zk.statement(other)["rows_root"]


def test_harm_claim_uses_the_precommitted_threshold_and_real_bound():
    observed = witness(2000, kind="harm")
    statement = zk.statement(observed)
    assert statement["harm_claim"] and statement["upper_ppm"] < 100_000
    for row in observed["rows"]:
        row["outcome"] = zk.SCALE
    assert not zk.statement(observed)["harm_claim"]


def test_noise_is_preregistered_hides_exact_statistics_and_has_explicit_conditional_bounds():
    observed = witness(2400, dp=True)
    report = zk.statement(observed)
    assert report == zk.statement(observed)
    assert report["hajek_ppm"] is None
    assert report["noise"]["epsilon_upper_ppm"] <= 20*zk.SCALE
    assert report["noise"]["delta_upper_power_of_two"] == -224
    assert report["estimate_ppm"] % zk.QUANTUM == 0
    assert observed["noise_seed"] not in json.dumps(report)
    assert observed["commitment_seed"] not in json.dumps(report)
    changed = deepcopy(observed)
    changed["noise_seed"] = "78"*32
    with pytest.raises(ValueError, match="seed"):
        zk.statement(changed)
    assert not zk.transparent(observed)["private_rows"]  # noisy arithmetic does not hide disclosed rows


def test_closed_profile_bounds_types_and_oversized_studies_fail_closed():
    for propensity in (True, 0, 49_999, 950_001, zk.SCALE):
        with pytest.raises(ValueError):
            zk.cohort([{"episode": "a", "propensity": propensity}, {"episode": "b", "propensity": 500_000}])
    with pytest.raises(ValueError, match="distinct"):
        zk.cohort([{"episode": "a", "propensity": 500_000}]*2)
    with pytest.raises(ValueError, match="chunked"):
        zk.cohort([{"episode": str(index), "propensity": 500_000} for index in range(30_001)])
    observed = witness()
    observed["rows"][0]["outcome"] = True
    with pytest.raises(ValueError):
        zk.statement(observed)


def test_chunk_extension_requires_complete_coverage_and_a_successful_external_verifier():
    observed = witness(2)
    plan = {**observed["plan"], "count": 30_001}
    question = zk.challenge(plan, "ab"*32)
    chunks = [{"start": 0, "rows": [None]*30_000}, {"start": 30_000, "rows": [None]}]
    with pytest.raises(zk.BackendUnavailable, match="provider"):
        zk.prove_chunked(plan, question, chunks, None)

    class RejectingProvider:
        calls = 0

        def prove_chunks(self, registered, challenge, chunks):
            self.calls += 1
            assert challenge == question
            return {"statement": {"count": 30_001, "plan_hash": fp.digest(registered),
                                  "challenge_hash": fp.digest(challenge)}}

        def verify(self, proof, expected_challenge):
            return False

    provider = RejectingProvider()
    with pytest.raises(ValueError, match="registered verifier"):
        zk.prove_chunked(plan, question, chunks, provider)
    assert provider.calls == 1
    chunks[1]["start"] -= 1
    with pytest.raises(ValueError, match="contiguous"):
        zk.prove_chunked(plan, question, chunks, provider)
    assert provider.calls == 1
