"""Bounded integer effect statements and complete-cohort Merkle witnesses.

The core backend discloses witness rows. Proofs establish arithmetic and protocol
commitments, never execution, outcome truth, random-seed entropy or delivery.
"""
from __future__ import annotations

import hashlib
import math
import re
import struct
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from commontrace import frontier_protocol as fp
from commontrace import transparency_log

SCALE = 1_000_000
MAX_ROWS = 30_000
MIN_PROPENSITY = 50_000
MAX_I128 = 2**127-1
# Rounded UP in units of 1e-6: ln(2/alpha), and ln(4/alpha) for a DP split.
LOG_UPPER = {50_000: (3_688_880, 4_382_027), 10_000: (5_298_318, 5_991_465),
             1_000: (7_600_903, 8_294_050), 1: (14_508_658, 15_201_805)}
DP_LOG_UPPER = {(3, 5): 510_826, (9, 10): 105_361, (99, 100): 10_051}
QUANTUM = 1_000
DP_RANDOM_BITS = 14_256
HEX = re.compile(r"[a-f0-9]{64}\Z")
IDENTITY = re.compile(r"[A-Za-z0-9_.:/-]{1,128}\Z")


class BackendUnavailable(RuntimeError):
    """An actual proof toolchain or registered verifier is required."""


class ChunkedBackend(Protocol):
    """Operator-provisioned recursive proof adapter; no built-in simulated receipt."""

    def prove_chunks(self, plan: Mapping[str, Any], challenge: Mapping[str, Any],
                     chunks: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]: ...
    def verify(self, proof: Mapping[str, Any], expected_challenge: str) -> bool: ...


def _hex(value: Any) -> str:
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError("UniMem proof commitments and seeds require canonical 32-byte hex")
    return value


def _id(value: Any) -> str:
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ValueError("UniMem proof episode IDs require 1..128 printable ASCII identifier bytes")
    return value


def _i128(value: int) -> int:
    if abs(value) > MAX_I128:
        raise ValueError("UniMem proof arithmetic exceeds signed 128-bit bounds")
    return value


def _ceil(numerator: int, denominator: int) -> int:
    return -(-numerator//denominator)


def _sqrt_ceil(value: int) -> int:
    root = math.isqrt(value)
    return root+(root*root != value)


def _identifier(value: str) -> bytes:
    raw = _id(value).encode("ascii")
    return struct.pack("<H", len(raw))+raw


def cohort(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    fp.count(len(entries), 2, MAX_ROWS, "proof cohort; larger studies require a chunked provider")
    result: list[dict[str, Any]] = []
    for entry in entries:
        if set(entry) != {"episode", "propensity"}:
            raise ValueError("UniMem proof cohort requires episode and integer propensity")
        result.append({"episode": _id(entry["episode"]),
                       "propensity": fp.count(entry["propensity"], MIN_PROPENSITY, SCALE-MIN_PROPENSITY)})
    result.sort(key=lambda row: row["episode"])
    if len({row["episode"] for row in result}) != len(result):
        raise ValueError("UniMem proof cohort episodes must be distinct")
    return result


def cohort_root(entries: Sequence[Mapping[str, Any]]) -> str:
    selected = cohort(entries)
    return transparency_log.tree_hash([hashlib.sha256(b"\x02"+_identifier(row["episode"])+
        struct.pack("<I", row["propensity"])).hexdigest() for row in selected])


def _dp_parameters(plan: Mapping[str, Any]) -> tuple[int, int, int]:
    dp = plan["dp"]
    if not isinstance(dp, dict) or set(dp) != {"numerator", "denominator", "noise_key"}:
        raise ValueError("UniMem proof noise profile has unknown or missing fields")
    fraction = (dp["numerator"], dp["denominator"])
    if any(type(value) is not int for value in fraction) or fraction not in DP_LOG_UPPER:
        raise ValueError("UniMem proof noise supports only the pinned finite-grid exponential profiles")
    _hex(dp["noise_key"])
    sensitivity = (_ceil(SCALE, plan["count"])+1 if plan["kind"] == "harm" else
                   _ceil(SCALE*SCALE, plan["count"]*plan["propensity_min"])+2)
    maximum = SCALE//QUANTUM if plan["kind"] == "harm" else 2*SCALE//QUANTUM
    buckets = min(maximum, _ceil(sensitivity, QUANTUM))
    epsilon = 2*buckets*DP_LOG_UPPER[fraction]
    if epsilon > 20*SCALE:
        raise ValueError("UniMem noise profile needs more observations or a stronger noise fraction")
    return fraction[0], fraction[1], epsilon


def plan(study: str, entries: Sequence[Mapping[str, Any]], *, commitment_key: str,
         alpha_ppm: int = 50_000, kind: str = "effect", harm_limit_ppm: int | None = None,
         dp: dict[str, Any] | None = None) -> dict[str, Any]:
    selected = cohort(entries)
    if type(alpha_ppm) is not int or alpha_ppm not in LOG_UPPER or kind not in {"effect", "harm"}:
        raise ValueError("UniMem proof supports effect/harm with alpha .05, .01, .001 or .000001")
    if kind == "harm":
        if harm_limit_ppm is None:
            raise ValueError("UniMem a harm statement needs a precommitted harm limit")
        fp.count(harm_limit_ppm, 0, SCALE, "precommitted harm limit")
    elif harm_limit_ppm is not None:
        raise ValueError("UniMem harm limits apply only to harm statements")
    result = {"schema": "unimem.effect-plan.v1", "study": _id(study), "scale": SCALE,
        "count": len(selected), "cohort_root": cohort_root(selected),
        "propensity_min": min(min(row["propensity"], SCALE-row["propensity"]) for row in selected),
        "alpha_ppm": alpha_ppm, "kind": kind, "harm_limit_ppm": harm_limit_ppm,
        "commitment_key": _hex(commitment_key), "dp": dp}
    if dp is not None:
        _dp_parameters(result)
    return result


def seed_key(seed: str, *, noise: bool = False) -> str:
    return hashlib.sha256((b"unimem-effect-noise-v1\x00" if noise else b"unimem-effect-hiding-v1\x00")+
                          bytes.fromhex(_hex(seed))).hexdigest()


def challenge(plan_value: Mapping[str, Any], nonce: str) -> dict[str, str]:
    return {"plan_hash": fp.digest(dict(plan_value)), "nonce": _hex(nonce)}


def assignment(plan_value: Mapping[str, Any], challenge_value: Mapping[str, Any], episode: str,
               propensity: int) -> bool:
    if set(challenge_value) != {"plan_hash", "nonce"} or challenge_value["plan_hash"] != fp.digest(dict(plan_value)):
        raise ValueError("UniMem verifier challenge is for another complete cohort")
    fp.count(propensity, MIN_PROPENSITY, SCALE-MIN_PROPENSITY)
    prefix = b"unimem-effect-assignment-v1\x00"+bytes.fromhex(_hex(challenge_value["nonce"]))+bytes.fromhex(
        _hex(challenge_value["plan_hash"]))+_identifier(episode)
    limit = (2**256//SCALE)*SCALE
    # Rejection avoids rounding a decimal propensity into a different Bernoulli
    # probability. The bound has failure probability below 2**-3700 in the RO model.
    for counter in range(16):
        value = int.from_bytes(hashlib.sha256(prefix+struct.pack("<I", counter)).digest(), "big")
        if value < limit:
            return value % SCALE < propensity
    raise ValueError("UniMem assignment rejection bound reached; no outcome may be recorded")


def _rows(witness: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    if set(witness) != {"plan", "challenge", "rows", "commitment_seed", "noise_seed"}:
        raise ValueError("UniMem proof witness has unknown or missing fields")
    submitted = witness["plan"]
    supplied = witness["rows"]
    if not isinstance(submitted, dict) or not isinstance(supplied, list):
        raise ValueError("UniMem proof plan and rows require canonical objects")
    fp.count(len(supplied), 2, MAX_ROWS, "proof witness row count")
    selected = cohort([{"episode": row["episode"], "propensity": row["propensity"]} for row in supplied])
    rebuilt = plan(submitted["study"], selected, commitment_key=submitted["commitment_key"],
        alpha_ppm=submitted["alpha_ppm"], kind=submitted["kind"], harm_limit_ppm=submitted["harm_limit_ppm"],
        dp=submitted["dp"])
    if fp.wire(rebuilt) != fp.wire(submitted):
        raise ValueError("UniMem proof rows do not match the complete preregistered cohort")
    seed = _hex(witness["commitment_seed"])
    if seed_key(seed) != submitted["commitment_key"]:
        raise ValueError("UniMem outcome commitment seed was not preregistered")
    if submitted["dp"] is None:
        if witness["noise_seed"] is not None:
            raise ValueError("UniMem unexpected private noise witness")
    elif seed_key(witness["noise_seed"], noise=True) != submitted["dp"]["noise_key"]:
        raise ValueError("UniMem private noise seed was not preregistered")
    ordered = sorted(supplied, key=lambda row: row["episode"])
    leaves = []
    for index, row in enumerate(ordered):
        if set(row) != {"episode", "propensity", "assigned", "outcome"} or type(row["assigned"]) is not bool:
            raise ValueError("UniMem proof rows require exact episode, propensity, assignment and integer outcome")
        fp.count(row["outcome"], 0, SCALE, "fixed-point outcome")
        if row["assigned"] != assignment(submitted, witness["challenge"], row["episode"], row["propensity"]):
            raise ValueError("UniMem proof assignment differs from the verifier nonce")
        encoded = _identifier(row["episode"])+struct.pack("<?II", row["assigned"], row["propensity"], row["outcome"])
        leaves.append(hashlib.sha256(b"\x00"+bytes.fromhex(seed)+struct.pack("<I", index)+encoded).hexdigest())
    return rebuilt, ordered, transparency_log.tree_hash(leaves)


def _radius(plan_value: Mapping[str, Any], *, dp: bool = False) -> int:
    width = (SCALE if plan_value["kind"] == "harm" else
             _ceil(SCALE*SCALE, plan_value["propensity_min"])+
             _ceil(SCALE*SCALE, SCALE-plan_value["propensity_min"]))
    numerator = _i128(width*width*LOG_UPPER[plan_value["alpha_ppm"]][int(dp)])
    return _sqrt_ceil(_ceil(numerator, 2*plan_value["count"]*SCALE))


def _noise(plan_value: Mapping[str, Any], value: int, seed: str) -> tuple[int, int, int]:
    numerator, denominator, epsilon = _dp_parameters(plan_value)
    low = 0 if plan_value["kind"] == "harm" else -SCALE
    maximum = (SCALE-low)//QUANTUM
    center = (max(low, min(SCALE, value))-low)//QUANTUM
    weights = [pow(numerator, abs(index-center))*pow(denominator, maximum-abs(index-center))
               for index in range(maximum+1)]
    total = sum(weights)
    if total.bit_length() > 14_000:
        raise ValueError("UniMem finite noise weights exceed their documented 14000-bit bound")
    # A fixed public bit length avoids a data-dependent rejection/timing loop.
    # Modulo bias is <=2**-256 TV; epsilon<=20 yields delta<2**-224.
    prefix = b"unimem-effect-dp-draw-v1\x00"+bytes.fromhex(_hex(seed))+bytes.fromhex(fp.digest(dict(plan_value)))
    raw = b"".join(hashlib.sha256(prefix+struct.pack("<I", index)).digest()
                   for index in range(_ceil(DP_RANDOM_BITS, 256)))
    draw = (int.from_bytes(raw, "big") & ((1 << DP_RANDOM_BITS)-1)) % total
    selected = 0
    for index, weight in enumerate(weights):
        if draw < weight:
            selected = index
            break
        draw -= weight
    tail = maximum
    for distance in range(maximum):
        power = distance+1
        if 8*SCALE*pow(numerator, power)*denominator <= (
                plan_value["alpha_ppm"]*(denominator-numerator)*pow(denominator, power)):
            tail = distance
            break
    return low+selected*QUANTUM, tail*QUANTUM+QUANTUM, epsilon


def statement(witness: Mapping[str, Any]) -> dict[str, Any]:
    registered, rows, root = _rows(witness)
    treated, control, treated_weight, control_weight, scores = 0, 0, 0, 0, []
    for row in rows:
        p, y = row["propensity"], row["outcome"]
        if registered["kind"] == "harm":
            scores.append(y)
        elif row["assigned"]:
            score = y*SCALE//p
            treated += score
            treated_weight += SCALE*SCALE//p
            scores.append(score)
        else:
            score = y*SCALE//(SCALE-p)
            control += score
            control_weight += SCALE*SCALE//(SCALE-p)
            scores.append(-score)
    estimate = _i128(sum(scores))//len(rows)
    hajek = (SCALE*treated//treated_weight-SCALE*control//control_weight
             if treated_weight and control_weight else None)
    radius = _radius(registered)
    rounding = 1 if registered["kind"] == "harm" else 2
    noise_profile = None
    if registered["dp"] is not None:
        estimate, noise_radius, epsilon = _noise(registered, estimate, witness["noise_seed"])
        radius = _radius(registered, dp=True)+noise_radius
        hajek = None  # Never leak an exact statistic beside the noisy release.
        noise_profile = {"mechanism": "finite-grid exponential", "epsilon_upper_ppm": epsilon,
            "delta_upper_power_of_two": -224, "quantum_ppm": QUANTUM,
            "conditions": "secret independent seeds, cryptographic hiding/PRG assumptions and one release"}
    low = 0 if registered["kind"] == "harm" else -SCALE
    lower = max(low, min(SCALE, estimate-radius-rounding))
    upper = max(low, min(SCALE, estimate+radius+rounding))
    return {"schema": "unimem.effect-statement.v1", "plan_hash": fp.digest(registered),
        "challenge_hash": fp.digest(witness["challenge"]), "rows_root": root, "count": len(rows),
        "kind": registered["kind"], "scale": SCALE, "alpha_ppm": registered["alpha_ppm"],
        "estimate_ppm": estimate, "hajek_ppm": hajek, "hajek_rounding_error_ppm": 6 if hajek is not None else None,
        "lower_ppm": lower, "upper_ppm": upper, "rounding_error_ppm": rounding,
        "confidence": "bounded fixed-cohort Hoeffding; assignment and sampling assumptions required",
        "harm_limit_ppm": registered["harm_limit_ppm"],
        "harm_claim": upper <= registered["harm_limit_ppm"] if registered["kind"] == "harm" else None,
        "noise": noise_profile, "execution_attested": False}


def transparent(witness: Mapping[str, Any]) -> dict[str, Any]:
    return {"backend": "transparent", "statement": statement(witness), "private_rows": False,
            "verification": "complete witness disclosure and audited recomputation; no input privacy"}


def verify_transparent(proof: Mapping[str, Any], witness: Mapping[str, Any], expected_challenge: str) -> bool:
    try:
        if fp.digest(witness["challenge"]) != _hex(expected_challenge):
            return False
        return fp.wire(dict(proof)) == fp.wire(transparent(witness))
    except (ValueError, TypeError, KeyError, OverflowError):
        return False


def prove_chunked(plan_value: Mapping[str, Any], challenge_value: Mapping[str, Any],
                  chunks: Sequence[Mapping[str, Any]], provider: ChunkedBackend | None) -> Mapping[str, Any]:
    """Delegate an entire larger cohort to an explicitly supplied recursive backend.

    No generic callback is advertised as a built-in private proof system. The
    operator must pin and audit its provider and its cryptographic verifier.
    """
    if provider is None:
        raise BackendUnavailable("UniMem a recursive circuit provider is required; no substitute receipt exists")
    fp.count(plan_value["count"], MAX_ROWS+1, 1_000_000, "chunked cohort")
    fp.count(len(chunks), 2, 64, "recursive proof chunks")
    expected = challenge(plan_value, challenge_value["nonce"])
    if dict(challenge_value) != expected:
        raise ValueError("UniMem chunked proof challenge differs from its pinned cohort")
    covered = 0
    for chunk in chunks:
        if set(chunk) != {"start", "rows"} or chunk["start"] != covered:
            raise ValueError("UniMem recursive chunks must cover one contiguous complete cohort")
        fp.count(len(chunk["rows"]), 1, MAX_ROWS, "recursive chunk rows")
        covered += len(chunk["rows"])
    if covered != plan_value["count"]:
        raise ValueError("UniMem recursive chunks omit or exceed the complete registered cohort")
    proof = provider.prove_chunks(plan_value, expected, chunks)
    statement_value = proof.get("statement", {})
    if (not isinstance(statement_value, Mapping) or statement_value.get("count") != covered
            or statement_value.get("plan_hash") != expected["plan_hash"]
            or statement_value.get("challenge_hash") != fp.digest(expected)
            or not provider.verify(proof, fp.digest(expected))):
        raise ValueError("UniMem external recursive proof failed its registered verifier")
    return proof
