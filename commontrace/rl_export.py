"""UniMem matched randomized-arm training records and causal-priority replay.

DPO (arXiv:2305.18290), Agent Lightning (arXiv:2508.03680), RL's Razor
(arXiv:2509.04259). This is a data broker, not a training algorithm. Matched
records are different randomized occasions, never individual counterfactuals.
DR control-potential baselines use public fixed nuisances: Y minus that baseline
is unbiased for improvement over the control policy in the logged mixture,
under exact randomization, bounded outcomes and noninformative selection.
Replay probabilities and sampled token KL are estimates/heuristics, not release
certificates or a guarantee on a trained model's KL or performance.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from commontrace import frontier_protocol as fp

MAX_EPISODES = 2048
FORMATS = ("dpo", "advantage", "replay")
VENDOR_SCHEMA_STATUS = "unverified against vendor schema"
CONNECTORS = ("generic", "agent-lightning", "grader")


def validate(episode: dict[str, Any]) -> None:
    fp.wire(episode)
    for field in ("occasion_id", "unit", "cluster", "context", "trace_id"):
        fp.text(episode[field], field, 128)
    fp.text(episode["prompt"], "training prompt", 20000)
    fp.text(episode["response"], "observed response", 20000)
    fp.number(episode["rate"], .001, .999)
    if type(episode["injected"]) is not bool or type(episode["outcome"]) is not bool:
        raise ValueError("UniMem training records require binary canonical assignment and outcome")
    fp.count(len(episode["scopes"]), 0, 128)
    for scope in episode["scopes"]:
        fp.text(scope, "scope", 256)
    fp.text(episode["integrity"], "integrity", 128)
    confidentiality = episode["confidentiality"]
    if isinstance(confidentiality, list):
        fp.count(len(confidentiality), 0, 128)
        for label in confidentiality:
            fp.text(label, "confidentiality label", 256)
    else:
        fp.text(confidentiality, "confidentiality", 128)


def token_kl(policy: Sequence[float] | None, base: Sequence[float] | None, budget: float) -> dict[str, Any]:
    """Mean sampled log likelihood ratio; not the exact distributional KL.

    A single sampled trajectory may have a negative log ratio. Preserve it and
    state the estimator, rather than clipping it and calling it a KL guarantee.
    """
    fp.number(budget, 0, 100)
    if policy is None and base is None:
        return {"status": "unchecked", "budget": budget, "estimate": None}
    if policy is None or base is None or len(policy) != len(base):
        raise ValueError("UniMem KL checking requires matching policy and base token logprobs")
    fp.count(len(policy), 1, 8192)
    differences = [fp.number(p, -1000, 0, "token logprob") - fp.number(b, -1000, 0, "base token logprob")
                   for p, b in zip(policy, base)]
    estimate = math.fsum(differences)/len(differences)
    return {"status": "within sampled budget" if estimate <= budget else "over sampled budget",
            "budget": budget, "estimate": estimate, "classification": "estimate",
            "assumptions": ["tokens sampled from the declared policy", "authenticated aligned token logs"],
            "method": "mean sampled token log(policy/base); not exact distributional KL"}


def advantage(episode: dict[str, Any], *, m0: float = .5, m1: float = .5) -> dict[str, Any]:
    validate(episode)
    fp.number(m0, 0, 1, "frozen public control prediction")
    fp.number(m1, 0, 1, "frozen public treatment prediction")
    y, z, p = int(episode["outcome"]), int(episode["injected"]), 1-episode["rate"]
    baseline = m0 + (1-z)*(y-m0)/(1-p)
    treatment = m1 + z*(y-m1)/p
    return {"occasion_id": episode["occasion_id"], "prompt": episode["prompt"], "response": episode["response"],
            "outcome": y, "baseline": baseline, "advantage": y-baseline,
            "aipw_effect_score": treatment-baseline, "propensity": p,
            "meta": {"unit": episode["unit"], "segment": episode["context"], "scopes": episode["scopes"],
                     "integrity": episode["integrity"], "confidentiality": episode["confidentiality"],
                     "interpretation": "logged-policy improvement over control; not an individual counterfactual",
                     "nuisance": [m0, m1], "classification": "estimate"}}


def priorities(episodes: Sequence[dict[str, Any]], intervals: dict[str, Sequence[float]], *,
               exploitation: float = .8, exploration: float = .2) -> list[dict[str, Any]]:
    fp.count(len(episodes), 0, MAX_EPISODES)
    fp.number(exploitation, 0, 1)
    fp.number(exploration, 0, 1)
    if exploitation + exploration <= 0:
        raise ValueError("UniMem replay needs a positive exploitation or exploration weight")
    weights = []
    for episode in episodes:
        validate(episode)
        low, high = intervals[episode["unit"]]
        fp.number(low, -1, 1)
        fp.number(high, low, 1)
        width = high-low
        weights.append((exploitation*max(0.0, low) + exploration)*width)
    total = math.fsum(weights)
    return [{"occasion_id": e["occasion_id"], "weight": w,
             "probability": w/total if total else 1/len(episodes),
             "classification": "heuristic", "fallback": "uniform" if not total else None}
            for e, w in zip(episodes, weights)]


def sample(episodes: Sequence[dict[str, Any]], intervals: dict[str, Sequence[float]], *,
           count: int, seed: int, exploitation: float = .8, exploration: float = .2) -> list[str]:
    fp.count(count, 1, 4096)
    fp.count(seed, 0, 2**63-1)
    if not episodes:
        raise ValueError("UniMem replay cannot sample an empty cohort")
    weights = priorities(episodes, intervals, exploitation=exploitation, exploration=exploration)
    return random.Random(seed).choices([r["occasion_id"] for r in weights],
                                       weights=[r["probability"] for r in weights], k=count)


def preferences(episodes: Sequence[dict[str, Any]], effects: dict[str, dict[str, Any]], *,
                kl_budget: float = .1) -> list[dict[str, Any]]:
    fp.count(len(episodes), 0, MAX_EPISODES)
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    seen = set()
    for episode in episodes:
        validate(episode)
        identity = episode["occasion_id"], episode["unit"]
        if identity in seen:
            raise ValueError("UniMem preference episodes must be distinct")
        seen.add(identity)
        labels = tuple(sorted(episode["scopes"])), episode["integrity"], fp.digest(episode["confidentiality"])
        key = episode["unit"], episode["cluster"], episode["context"], fp.digest(episode["prompt"]), labels
        grouped[key].append(episode)
    result = []
    for _, group in sorted(grouped.items(), key=lambda item: str(item[0])):
        arms = {z: sorted([e for e in group if e["injected"] is z], key=lambda e: e["occasion_id"])
                for z in (False, True)}
        # Disjoint pairs prevent one convenient control response from being
        # duplicated into artificial independent training evidence.
        for treated, control in zip(arms[True], arms[False]):
            if treated["outcome"] == control["outcome"]:
                continue
            chosen, rejected = (treated, control) if treated["outcome"] else (control, treated)
            kl = [token_kl(e.get("policy_logprobs"), e.get("base_logprobs"), kl_budget) for e in (chosen, rejected)]
            if any(k["status"] == "over sampled budget" for k in kl):
                raise PermissionError("UniMem preference pair exceeds its sampled token KL budget")
            effect = effects[chosen["unit"]]
            result.append({"prompt": chosen["prompt"], "chosen": chosen["response"], "rejected": rejected["response"],
                "meta": {"pairing": "different randomized occasions matched on observed task features",
                    "same_instance_counterfactual": False, "population_effect": effect["effect"],
                    "interval": effect["interval"], "segment": chosen["context"], "cluster": chosen["cluster"],
                    "unit": chosen["unit"], "occasions": [chosen["occasion_id"], rejected["occasion_id"]],
                    "propensities": [1-chosen["rate"], 1-rejected["rate"]], "kl_to_base": kl,
                    "scopes": chosen["scopes"], "integrity": chosen["integrity"],
                    "confidentiality": chosen["confidentiality"], "classification": "estimate",
                    "assumptions": ["exact randomization; complete outcomes", "matching uses pre-treatment features",
                                    "cohort selection is noninformative; no interference"],
                    "vendor_schema": VENDOR_SCHEMA_STATUS}})
    return result


def connect(record: dict[str, Any], connector: str) -> dict[str, Any]:
    """Versioned adapter shapes; no SDK or vendor network dependency.

    These mappings need upstream schema fixtures before any integration claims
    beyond the documented JSON shape. An external grader must authenticate its
    own endpoint and independently evaluate the response.
    """
    if connector not in CONNECTORS:
        raise ValueError("UniMem connector must be generic, agent-lightning or grader")
    if connector == "generic":
        return record
    fp.wire(record)
    metadata = {"unimem": record.get("meta", {}), "schema_status": VENDOR_SCHEMA_STATUS,
                "adapter_version": 1, "classification": "estimate"}
    if connector == "agent-lightning":
        return {"state": {"input": record["prompt"]},
                "action": {"output": record.get("response", record.get("chosen"))},
                "reward": record.get("outcome"), "training_record": record, "metadata": metadata}
    return {"item": {"prompt": record["prompt"], "training_record": record},
            "sample": {"output_text": record.get("response", record.get("chosen"))}, "metadata": metadata}
