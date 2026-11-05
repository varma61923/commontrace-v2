"""Graded and multi-objective outcomes for any agent, model or robot.

The causal engine records one binary outcome per occasion. Real outcomes are
graded and several at once. A robot finishes 70% of a pick sequence and uses
30 Wh. A model's answer scores 0.8 with an evaluator and costs 2,400 tokens. A
coding agent's patch passes 9 of 12 tests in 40 seconds. This module brings
those outcomes into the same evidence chain without changing any estimator.

**Randomized rounding.** A reward ``r`` in ``[0, 1]`` becomes the canonical
outcome ``succeeded = U < r``. ``U = H(salt, occasion) / 2**64`` is a
domain-separated SHA-256 draw that any auditor holding the salt can reproduce.
It does not depend on which memories were shown (assignment hashes use a
different domain) and is fixed before the reward is known. So
``P(succeeded | arm) = E[r | arm]``, and everything downstream (Hajek and AIPW
effects, e-processes, harm withdrawal, the causal gate and the effect field)
estimates effects on expected reward without bias. The cost is variance: a
rounded reward is a Bernoulli draw with the same mean, never noisier than an
outcome that was binary in the first place. `effects` gives a lower-variance
companion estimate directly on the reward scale.

**Utility.** A configured utility turns named measurements into one reward. It
takes a positive-weighted mean of terms, each scaled to ``[0, 1]`` between
declared bounds in a declared direction (``higher`` or ``lower`` is better). A
missing term is an error, never imputed. Each reward stores the utility's
digest, so a change of value function mid-experiment shows up in `verify`.

**Audit.** `verify` recomputes every draw and checks each recorded bit against
the outcomes log, so binary outcomes cannot be quietly chosen instead of
derived. A reward never overwrites an outcome. A different reward for an
occasion already on record is a conflict, as it is for binary outcomes.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from unimem import _jsonl, holdout_io, paths

DOMAIN = b"unimem/reward-rounding/v1"
MAX_TERMS = 32
MAX_METRICS = 64
TERM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
BETTER = ("higher", "lower")
REWARDS_NAME = "rewards.jsonl"
UTILITY_NAME = "utility.json"


def _dir(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "rewards")


def rewards_path(root: str) -> str:
    return os.path.join(_dir(root), REWARDS_NAME)


def utility_path(root: str) -> str:
    return os.path.join(_dir(root), UTILITY_NAME)


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fraction(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{label} must be between 0 and 1")
    return float(value)


# -- randomized rounding -------------------------------------------------------------------------


def draw(salt: str, occasion_id: str) -> float:
    """The occasion's uniform rounding draw in ``[0, 1)``, reproducible from the salt."""
    h = hashlib.sha256(DOMAIN + b"\0" + salt.encode("utf-8") + b"\0" + occasion_id.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") / 2.0**64


def realize(salt: str, occasion_id: str, reward: float) -> bool:
    """The canonical binary outcome for `reward`: true with probability exactly `reward`."""
    return draw(salt, occasion_id) < _fraction(reward, "reward")


def salt_digest(salt: str) -> str:
    return "sha256:" + hashlib.sha256(DOMAIN + b"\0salt\0" + salt.encode("utf-8")).hexdigest()[:32]


# -- utility -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Term:
    weight: float
    low: float
    high: float
    better: str = "higher"

    def scale(self, value: float) -> float:
        x = min(1.0, max(0.0, (value - self.low) / (self.high - self.low)))
        return x if self.better == "higher" else 1.0 - x


def parse_utility(spec: Mapping[str, Any]) -> dict[str, Term]:
    """Validated utility terms from ``{"terms": {name: {weight, low, high, better}}}``."""
    terms = spec.get("terms") if isinstance(spec, Mapping) else None
    if not isinstance(terms, Mapping) or not 1 <= len(terms) <= MAX_TERMS:
        raise ValueError(f"utility needs 1 to {MAX_TERMS} terms")
    out: dict[str, Term] = {}
    for name, raw in sorted(terms.items()):
        if not isinstance(name, str) or not TERM_RE.match(name):
            raise ValueError(f"utility term name {name!r} must be a short identifier")
        if not isinstance(raw, Mapping) or set(raw) - {"weight", "low", "high", "better"}:
            raise ValueError(f"utility term {name!r} takes weight, low, high and better")
        values = {}
        for key, default in (("weight", 1.0), ("low", 0.0), ("high", 1.0)):
            value = raw.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"utility term {name!r} {key} must be a finite number")
            values[key] = float(value)
        better = raw.get("better", "higher")
        if better not in BETTER:
            raise ValueError(f"utility term {name!r} better must be higher or lower")
        if values["weight"] <= 0 or not values["low"] < values["high"]:
            raise ValueError(f"utility term {name!r} needs a positive weight and low < high")
        out[name] = Term(values["weight"], values["low"], values["high"], better)
    return out


def _canonical(terms: Mapping[str, Term]) -> dict[str, Any]:
    return {
        "terms": {
            n: {"weight": t.weight, "low": t.low, "high": t.high, "better": t.better} for n, t in sorted(terms.items())
        }
    }


def _metrics(metrics: Mapping[str, Any]) -> dict[str, float]:
    """Every metric as a finite float under a name of 1 to 64 characters; booleans count as 0 or 1."""
    if not isinstance(metrics, Mapping) or len(metrics) > MAX_METRICS:
        raise ValueError(f"metrics must be an object of at most {MAX_METRICS} numbers")
    out = {}
    for name, value in metrics.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 64:
            raise ValueError("metric names must be strings of 1 to 64 characters")
        if not isinstance(value, (bool, int, float)) or not math.isfinite(value):
            raise ValueError(f"metric {name!r} must be a finite number")
        out[name] = float(value)
    return out


def score(terms: Mapping[str, Term], metrics: Mapping[str, Any]) -> float:
    """The weighted reward in ``[0, 1]``; every term must be measured and every metric numeric."""
    values = _metrics(metrics)
    missing = sorted(set(terms) - set(values))
    if missing:
        raise ValueError("metrics are missing utility terms: " + ", ".join(missing))
    total = math.fsum(t.weight for t in terms.values())
    return min(1.0, max(0.0, math.fsum(t.weight * t.scale(values[n]) for n, t in terms.items()) / total))


def configure(root: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    """Store the utility. Its digest is stamped on every reward scored with it."""
    canonical = _canonical(parse_utility(spec))
    out = {**canonical, "digest": _digest(canonical)}
    _jsonl.write_json(utility_path(root), out)
    return out


def utility(root: str) -> tuple[dict[str, Term], str] | None:
    """The configured utility terms and digest, or None."""
    try:
        with open(utility_path(root), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return None
    terms = parse_utility(raw)
    return terms, _digest(_canonical(terms))


# -- recording -----------------------------------------------------------------------------------


_INDEX: dict[str, tuple[int, int, dict[str, dict[str, Any]]]] = {}  # path -> (inode, parsed bytes, rows)


def load(root: str) -> dict[str, dict[str, Any]]:
    """Every recorded reward by occasion (the first record wins). Appends are parsed incrementally."""
    path = rewards_path(root)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        _INDEX.pop(path, None)
        return {}
    cached = _INDEX.get(path)
    if cached is not None and cached[0] == st.st_ino and cached[1] <= st.st_size:
        offset, out = cached[1], dict(cached[2])
    else:
        offset, out = 0, {}
    if offset < st.st_size:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
        complete = data.rfind(b"\n") + 1
        for line in data[:complete].splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            occasion = row.get("occasion_id") if isinstance(row, dict) else None
            if isinstance(occasion, str) and occasion not in out:
                out[occasion] = row
        offset += complete
    _INDEX[path] = (st.st_ino, offset, out)
    return dict(out)


def record(
    root: str,
    occasion_id: str,
    *,
    reward: float | None = None,
    metrics: Mapping[str, Any] | None = None,
    durable: bool = True,
    secondary_metrics: dict[str, float | bool] | None = None,
) -> dict[str, Any]:
    """Record a graded outcome (`reward`, or `metrics` scored by the utility) for `occasion_id`.

    `secondary_metrics` are passed to the outcomes log exactly as for a binary outcome.
    """
    if (reward is None) == (metrics is None):
        raise ValueError("give exactly one of reward or metrics")
    if not isinstance(occasion_id, str) or not occasion_id.strip():
        raise ValueError("occasion_id must be a non-empty string")
    from unimem import frontier_protocol as fp

    fp.text(occasion_id, "occasion")
    utility_digest = None
    if metrics is not None:
        configured = utility(root)
        if configured is None:
            raise ValueError("metrics need a configured utility (unimem reward configure)")
        terms, utility_digest = configured
        reward = score(terms, metrics)
    assert reward is not None
    reward = _fraction(reward, "reward")
    salt = holdout_io.load_config(root).salt
    u = draw(salt, occasion_id)
    succeeded = u < reward
    path = rewards_path(root)
    with _jsonl.locked(path):
        existing = load(root).get(occasion_id)
        if existing is not None:
            if abs(float(existing.get("reward", -1.0)) - reward) <= 1e-12:
                return {
                    "occasion_id": occasion_id,
                    "reward": reward,
                    "succeeded": bool(existing["succeeded"]),
                    "recorded": False,
                }
            raise holdout_io.ConflictingOutcome(
                f"occasion {occasion_id!r} already has reward {existing.get('reward')}; refusing to change it"
            )
        if occasion_id in holdout_io.read_outcomes(root):
            raise holdout_io.ConflictingOutcome(
                f"occasion {occasion_id!r} already has a binary outcome; a reward cannot be attached afterwards"
            )
        if not holdout_io.record_outcome(root, occasion_id, succeeded, durable, secondary_metrics=secondary_metrics):
            raise holdout_io.ConflictingOutcome(
                f"occasion {occasion_id!r} received a binary outcome concurrently; the reward was not attached"
            )
        row: dict[str, Any] = {
            "occasion_id": occasion_id,
            "reward": reward,
            "draw": u,
            "succeeded": succeeded,
            "salt_digest": salt_digest(salt),
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if metrics is not None:
            row["metrics"] = dict(sorted(_metrics(metrics).items()))
            row["utility_digest"] = utility_digest
        _jsonl.append_row(path, row, fsync=durable)
    return {"occasion_id": occasion_id, "reward": reward, "succeeded": succeeded, "recorded": True}


# -- audit and reward-scale effects --------------------------------------------------------------


def verify(root: str) -> list[str]:
    """Problems with the recorded rewards; empty when every bit is the rounding of its reward."""
    salt = holdout_io.load_config(root).salt
    current = salt_digest(salt)
    outcomes = holdout_io.read_outcomes(root)
    problems: list[str] = []
    digests: set[str] = set()
    for occasion, row in sorted(load(root).items()):
        if row.get("salt_digest") != current:
            continue
        try:
            expected = realize(salt, occasion, row["reward"])
            recorded_draw = float(row["draw"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"{occasion}: unreadable reward")
            continue
        if bool(row.get("succeeded")) != expected or abs(recorded_draw - draw(salt, occasion)) > 1e-12:
            problems.append(f"{occasion}: recorded bit is not the rounding of its reward")
        if outcomes.get(occasion) != expected:
            problems.append(f"{occasion}: outcomes log disagrees with the rounded reward")
        if row.get("utility_digest"):
            digests.add(str(row["utility_digest"]))
    if len(digests) > 1:
        problems.append(f"{len(digests)} different utilities scored this experiment's rewards")
    return problems


def effects(root: str) -> list[dict[str, Any]]:
    """Per-memory Hajek effects on the raw reward, over occasions that recorded one."""
    from unimem import causal_gate

    rewards = {k: float(v["reward"]) for k, v in load(root).items() if "reward" in v}
    arms: dict[str, dict[bool, list[tuple[float, float]]]] = defaultdict(lambda: {True: [], False: []})
    for r in causal_gate.current_rows(root):
        if r.occasion_id in rewards:
            arms[r.lesson][r.injected].append((1.0 / r.propensity, rewards[r.occasion_id]))
    out = []
    for lesson, by_arm in sorted(arms.items()):
        stats = []
        for injected in (True, False):
            rows = by_arm[injected]
            total = math.fsum(w for w, _y in rows)
            if not rows or total <= 0:
                break
            mean = math.fsum(w * y for w, y in rows) / total
            var = math.fsum((w * (y - mean)) ** 2 for w, y in rows) / total**2
            stats.append((mean, var, len(rows)))
        if len(stats) < 2:
            continue
        (m1, v1, n1), (m0, v0, n0) = stats
        se = math.sqrt(v1 + v0)
        out.append(
            {
                "lesson": lesson,
                "effect": m1 - m0,
                "se": se,
                "ci_low": m1 - m0 - 1.96 * se,
                "ci_high": m1 - m0 + 1.96 * se,
                "mean_injected": m1,
                "mean_withheld": m0,
                "n_injected": n1,
                "n_withheld": n0,
            }
        )
    return out
