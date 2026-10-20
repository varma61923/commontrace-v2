"""UniMem random-effects evidence pooling from bounded sufficient statistics.

DerSimonian and Laird (1986), doi:10.1016/0197-2456(86)90046-2;
modified Hartung-Knapp, doi:10.1186/s12874-015-0091-1 (arXiv:1508.01227).
Intervals are model-based estimates, not anytime confidence sequences. The
combined e-value tests the joint directional null, not a heterogeneous pooled
mean. Its product requires conditional independence between ownership groups.

Ownership groups must come from an operator's pinned identity registry. Shards
are combined within a group before fitting, so account splitting creates no
additional independent studies. No episode text or private memory IDs are used.
Gaussian releases protect replacement of one clipped score, conditional on a
fixed public cohort, assignment and frozen public nuisance model. Membership,
counts, unit fingerprints and public identities are outside that adjacency.
"""
from __future__ import annotations

import math
import random
import re
from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache
from statistics import NormalDist
from typing import Any, Sequence

from commontrace import aipw, eprocess, origin
from commontrace import frontier_protocol as fp

DOMAIN = "unimem.pooled-evidence"
VERSION = 1
MAX_TENANTS = 128
ARMS = ("treated", "control")


def fingerprint(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("UniMem units require a public SHA256 content fingerprint")
    return value


def gaussian_sigma(epsilon: float, delta: float, sensitivity: float) -> float:
    """Classical Gaussian calibration for epsilon <= 1 (Dwork & Roth, 2014).

    doi:10.1561/0400000042, Appendix A. The reference sampler uses finite-precision
    stdlib Gaussian sampling; this is not an audited exact-arithmetic DP engine.
    """
    fp.number(epsilon, .001, 1, "epsilon")
    fp.number(delta, 1e-12, .1, "delta")
    fp.number(sensitivity, 1e-9, 1e9, "L2 sensitivity")
    return sensitivity * math.sqrt(2 * math.log(1.25 / delta)) / epsilon


def summarize(tenant: str, unit_hash: str, rows: Sequence[dict[str, Any]], *, release_id: str,
              propensity_floor: float = .05, nuisance: tuple[float, float] = (.5, .5),
              epsilon: float | None = None, delta: float | None = None) -> dict[str, Any]:
    """Compute frozen-nuisance AIPW sums, squares and public counts per arm.

    Do not fit nuisances on these private outcomes before requesting DP: changing
    one outcome could then change every subsequent score. Frozen public means
    preserve both the stated sensitivity and randomization unbiasedness.
    """
    fp.text(tenant, "contributor", 128)
    fingerprint(unit_hash)
    fp.text(release_id, "release identity", 128)
    fp.count(len(rows), 2, fp.MAX_ROWS)
    floor = fp.number(propensity_floor, .001, .5, "declared propensity floor")
    if len(nuisance) != 2:
        raise ValueError("two frozen public arm predictions required")
    m0, m1 = (fp.number(x, 0, 1, "public nuisance prediction") for x in nuisance)
    bound = 1 / floor
    arms: dict[str, dict[str, Any]] = {arm: {"count": 0, "score_sum": 0.0, "square_sum": 0.0} for arm in ARMS}
    observations: list[aipw.Observation] = []
    seen: set[str] = set()
    for row in rows:
        identity = fp.text(row["occasion_id"])
        if identity in seen:
            raise ValueError("one outcome per randomized occasion required")
        seen.add(identity)
        if type(row["injected"]) is not bool or type(row["outcome"]) is not bool:
            raise ValueError("binary logged assignment and outcome required")
        p = fp.number(1 - row["rate"], floor, 1 - floor, "injection propensity")
        score = m1 - m0 + ((int(row["outcome"]) - m1) / p if row["injected"]
                           else -(int(row["outcome"]) - m0) / (1 - p))
        fp.number(score, -bound, bound, "AIPW score")
        arm = arms["treated" if row["injected"] else "control"]
        arm["count"] += 1
        arm["score_sum"] += score
        arm["square_sum"] += score * score
        observations.append(aipw.Observation(row["injected"], row["outcome"], row["rate"]))
    if any(arms[arm]["count"] == 0 for arm in ARMS):
        raise ValueError("both randomized arms need observations")
    body: dict[str, Any] = {"type": DOMAIN, "version": VERSION, "tenant": tenant, "unit_hash": unit_hash,
        "release_id": release_id, "arms": arms, "score_bound": bound, "noise_variance": 0.0,
        "nuisance": [m0, m1], "e_value": eprocess.process(observations).e_value,
        "classification": "estimate", "privacy": None}
    if epsilon is not None or delta is not None:
        if epsilon is None or delta is None:
            raise ValueError("Gaussian releases require epsilon and delta")
        # A replacement changes one arm's sum by <=2B and square sum by <=B².
        sigma = gaussian_sigma(epsilon, delta, math.hypot(2 * bound, bound * bound))
        rng = random.SystemRandom()
        for arm in arms.values():
            arm["score_sum"] += rng.gauss(0, sigma)
            arm["square_sum"] += rng.gauss(0, sigma)
        body["noise_variance"] = sigma * sigma
        body["e_value"] = 1.0  # An exact private-data e-value would bypass the noise mechanism.
        body["privacy"] = {"mechanism": "Gaussian reference", "epsilon": epsilon, "delta": delta,
            "sigma": sigma, "adjacency": "one bounded score replacement; fixed public cohort and arm counts",
            "protected_fields": ["arm score sums", "arm squared-score sums"],
            "nuisance": "frozen public predictions", "numerics": "finite-precision reference sampler",
            "e_value": "withheld; constant one carries no private evidence"}
    return body


def validate(body: dict[str, Any]) -> None:
    fp.wire(body)
    required = {"type", "version", "tenant", "unit_hash", "release_id", "arms", "score_bound",
                "noise_variance", "nuisance", "e_value", "classification", "privacy"}
    if (not isinstance(body, dict) or set(body) != required or body["type"] != DOMAIN
            or type(body["version"]) is not int or body["version"] != VERSION
            or body["classification"] != "estimate"):
        raise ValueError("unsupported UniMem contribution envelope")
    fp.text(body["tenant"], "contributor", 128)
    fp.text(body["release_id"], "release identity", 128)
    fingerprint(body["unit_hash"])
    bound = fp.number(body["score_bound"], 2, 1000)
    noise = fp.number(body["noise_variance"], 0, 1e30)
    fp.number(body["e_value"], 0, 1e300)
    if not isinstance(body["nuisance"], list) or len(body["nuisance"]) != 2:
        raise ValueError("frozen public nuisance predictions required")
    for value in body["nuisance"]:
        fp.number(value, 0, 1)
    if not isinstance(body["arms"], dict) or set(body["arms"]) != set(ARMS):
        raise ValueError("treated and control aggregates required")
    n = 0
    for arm in body["arms"].values():
        if not isinstance(arm, dict) or set(arm) != {"count", "score_sum", "square_sum"}:
            raise ValueError("bounded counts, score sums and squared sums required")
        count = fp.count(arm["count"], 1, fp.MAX_ROWS)
        total = fp.number(arm["score_sum"], -1e20, 1e20)
        squares = fp.number(arm["square_sum"], -1e20 if noise else 0, 1e20)
        if not noise and (abs(total) > bound * count + 1e-8 or squares > bound**2 * count + 1e-8
                          or total * total > count * squares + 1e-6):
            raise ValueError("aggregate scores violate their declared bounds")
        n += count
    fp.count(n, 2, fp.MAX_ROWS)
    privacy = body["privacy"]
    if privacy is not None:
        if not isinstance(privacy, dict) or body["e_value"] != 1:
            raise ValueError("private releases cannot expose a raw e-value")
        sigma = gaussian_sigma(privacy["epsilon"], privacy["delta"], math.hypot(2 * bound, bound * bound))
        if (privacy.get("mechanism") != "Gaussian reference"
                or not math.isclose(fp.number(privacy["sigma"], 0, 1e15), sigma, rel_tol=1e-12)
                or not math.isclose(noise, sigma * sigma, rel_tol=1e-12)):
            raise ValueError("privacy noise variance does not match the declared mechanism")
    elif noise:
        raise ValueError("noise variance requires a privacy mechanism")


def _beta_fraction(a: float, b: float, x: float) -> float:
    """Evaluate the incomplete-beta continued fraction (NIST DLMF 8.17).

    Bounded iteration and a small-denominator guard keep malformed inputs from
    causing nontermination. This implements the mathematical expansion directly.
    """
    tiny = 1e-250
    c, d = 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    value = d
    for m in range(1, 201):
        for numerator in (m * (b - m) * x / ((a + 2*m - 1) * (a + 2*m)),
                          -(a + m) * (a + b + m) * x / ((a + 2*m) * (a + 2*m + 1))):
            d = 1 + numerator * d
            c = 1 + numerator / c
            d = 1 / (d if abs(d) > tiny else tiny)
            c = c if abs(c) > tiny else tiny
            change = c * d
            value *= change
        if abs(change - 1) < 1e-13:
            return value
    raise ValueError("Student-t quantile did not converge")


def _beta(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    factor = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                      + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return factor * _beta_fraction(a, b, x) / a
    return 1 - factor * _beta_fraction(b, a, 1 - x) / b


@lru_cache(maxsize=256)
def t_critical(degrees: int, alpha: float = .05) -> float:
    fp.count(degrees, 1, MAX_TENANTS - 1)
    fp.number(alpha, 1e-6, .5)
    lo, hi = 0.0, 2.0
    for _ in range(30):
        if _beta(degrees / 2, .5, degrees / (degrees + hi * hi)) <= alpha:
            break
        hi *= 2
    for _ in range(60):
        mid = (lo + hi) / 2
        if _beta(degrees / 2, .5, degrees / (degrees + mid * mid)) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def _fit(estimates: Sequence[tuple[float, float]], alpha: float) -> dict[str, Any]:
    k = len(estimates)
    weights = [1 / variance for _, variance in estimates]
    sw = math.fsum(weights)
    fixed = math.fsum(w * y for w, (y, _) in zip(weights, estimates)) / sw
    q = math.fsum(w * (y - fixed)**2 for w, (y, _) in zip(weights, estimates))
    c = sw - math.fsum(w*w for w in weights) / sw
    tau2 = max(0.0, (q - k + 1) / c) if c > 0 else 0.0
    random_weights = [1 / (variance + tau2) for _, variance in estimates]
    total = math.fsum(random_weights)
    effect = math.fsum(w * y for w, (y, _) in zip(random_weights, estimates)) / total
    dispersion = math.fsum(w * (y - effect)**2 for w, (y, _) in zip(random_weights, estimates)) / (k - 1)
    standard_error = math.sqrt(max(1.0, dispersion) / total)
    radius = t_critical(k - 1, alpha) * standard_error
    return {"effect": effect, "interval": [effect - radius, effect + radius], "tau_squared": tau2,
            "standard_error": standard_error, "hartung_knapp_scale": max(1.0, dispersion)}


def _estimate(shards: Sequence[dict[str, Any]]) -> tuple[float, float, int]:
    n = sum(arm["count"] for shard in shards for arm in shard["arms"].values())
    fp.count(n, 2, fp.MAX_ROWS)
    total = math.fsum(arm["score_sum"] for shard in shards for arm in shard["arms"].values())
    squares = math.fsum(arm["square_sum"] for shard in shards for arm in shard["arms"].values())
    variance = max(1 / n, (squares - total * total / n) / (n - 1)) / n
    variance += 2 * math.fsum(shard["noise_variance"] for shard in shards) / n**2
    return total / n, variance, n


def pool(contributions: Sequence[dict[str, Any]], *, groups: dict[str, str],
         alpha: float = .05, independent: bool = False) -> dict[str, Any]:
    """Combine authenticated contributions using operator-established ownership.

    The pure function expects the caller to have verified signatures and pinned
    `groups`. It does not accept a contribution's assertion of independence.
    """
    fp.number(alpha, 1e-6, .5)
    if type(independent) is not bool:
        raise ValueError("independence must be an explicit boolean assumption")
    fp.count(len(contributions), 2, 512)
    fingerprint(contributions[0]["unit_hash"])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    tenants: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for body in contributions:
        validate(body)
        if body["unit_hash"] != contributions[0]["unit_hash"]:
            raise ValueError("pool units by the same public content fingerprint")
        if body["tenant"] not in groups:
            raise PermissionError("contributor lacks an operator-established ownership group")
        identity = body["tenant"], body["release_id"]
        if identity in seen:
            raise ValueError("a replayed release is not independent evidence")
        seen.add(identity)
        grouped[fp.text(groups[body["tenant"]], "ownership group", 128)].append(body)
        tenants[body["tenant"]].append(body)
    fp.count(len(grouped), 2, MAX_TENANTS, "independent ownership groups")
    estimates: list[tuple[float, float]] = []
    powers: list[dict[str, Any]] = []
    evidence: list[float] = []
    for group, shards in sorted(grouped.items()):
        # Noise on each arm's sum is independent. Squared sums remain noisy
        # variance estimates; the model-based interval does not become exact DP inference.
        mean, variance, n = _estimate(shards)
        estimates.append((mean, variance))
        se = math.sqrt(variance)
        powers.append({"group": fp.digest(group), "n": n, "standard_error": se,
                       "minimum_detectable_effect": (NormalDist().inv_cdf(1 - alpha/2)
                                                      + NormalDist().inv_cdf(.8)) * se})
        evidence.append(eprocess.combine([s["e_value"] for s in shards]))
    result = _fit(estimates, alpha)
    z = NormalDist().inv_cdf(1 - alpha/2)
    tenant_power = []
    for tenant, shards in sorted(tenants.items()):
        _, variance, n = _estimate(shards)
        se = math.sqrt(variance)
        tenant_power.append({"tenant": fp.digest(tenant), "group": fp.digest(groups[tenant]), "n": n,
            "standard_error": se, "minimum_detectable_effect": (z + NormalDist().inv_cdf(.8)) * se})
    for item in powers + tenant_power:
        signal = abs(result["effect"]) / item["standard_error"]
        item["design_power_at_pooled_estimate"] = NormalDist().cdf(-z - signal) + 1 - NormalDist().cdf(z - signal)
    influence = []
    for index, item in enumerate(powers):
        remainder = [e for j, e in enumerate(estimates) if j != index]
        effect = _fit(remainder, alpha)["effect"] if len(remainder) > 1 else remainder[0][0]
        influence.append({"group": item["group"], "effect_without": effect, "change": effect - result["effect"]})
    tenant_influence = []
    for tenant in sorted(tenants):
        remainder = []
        for shards in grouped.values():
            remaining = [s for s in shards if s["tenant"] != tenant]
            if remaining:
                mean, variance, _ = _estimate(remaining)
                remainder.append((mean, variance))
        effect = _fit(remainder, alpha)["effect"] if len(remainder) > 1 else remainder[0][0]
        tenant_influence.append({"tenant": fp.digest(tenant), "effect_without": effect,
                                 "change": effect - result["effect"]})
    combined = eprocess.combine(evidence, independent=independent)
    mde = (t_critical(len(grouped) - 1, alpha) + NormalDist().inv_cdf(.8)) * result["standard_error"]
    return {**result, "type": DOMAIN + ".report", "version": VERSION,
        "unit_hash": contributions[0]["unit_hash"], "tenants": len({c["tenant"] for c in contributions}),
        "ownership_groups": len(grouped), "tenant_power": tenant_power, "group_power": powers,
        "leave_one_out": tenant_influence, "leave_one_group_out": influence,
        "minimum_detectable_effect": mde, "combined_e_value": combined,
        "e_value_combination": "independent product" if independent else "dependence-robust arithmetic mean",
        "e_value_null": "every contributing group's conditional directional effect is nonpositive",
        "verdict": "POSITIVE_ESTIMATE" if result["interval"][0] > 0 else "UNDERPOWERED",
        "classification": "estimate", "interval_method": "DerSimonian-Laird; modified Hartung-Knapp Student-t",
        "alpha": alpha, "assumptions": ["independent operator-established ownership groups",
            "approximately normal within-group estimates with correctly estimated variances",
            "random-effects population model; fixed public unit and cohort design",
            "exact randomization propensities and bounded outcomes; no within-trial interference"],
        "warnings": (["Few ownership groups: DL/HK intervals can under-cover with strongly unequal precisions."]
                     if len(grouped) < 10 else []),
        "release_certificate": False}


def verify_contribution(signed: dict[str, Any], *, card: dict[str, Any]) -> dict[str, Any]:
    fp.wire(signed)
    if not isinstance(signed, dict) or not isinstance(signed.get("record"), dict):
        raise ValueError("UniMem contribution requires an object envelope and record")
    if fp.moment(card["expires_at"]) <= fp.now():
        raise PermissionError("UniMem contributor registration expired")
    identity = origin.Principal(card["principal"], card["organization"], "operator", b"", "ed25519",
                                bytes.fromhex(card["public_key"]))
    if not origin.verify(signed, {identity.id: identity}, authority="operator"):
        raise PermissionError("UniMem contribution signature is not trusted")
    record = signed["record"]
    if (record.get("type") != "unimem.pooled-contribution" or type(record.get("version")) is not int
            or record.get("version") != 1
            or fp.moment(record["expires_at"]) <= fp.now() or fp.moment(record["issued_at"]) > fp.now()):
        raise PermissionError("UniMem contribution has an invalid domain or validity window")
    body: dict[str, Any] = record["contribution"]
    if (datetime.fromisoformat(fp.moment(record["expires_at"]))
            - datetime.fromisoformat(fp.moment(record["issued_at"]))
            > timedelta(days=7)):
        raise PermissionError("UniMem contribution validity exceeds seven days")
    validate(body)
    if body["tenant"] != card["tenant"]:
        raise PermissionError("UniMem contribution cannot impersonate another tenant")
    return body
