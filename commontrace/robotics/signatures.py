"""Robust phase/payload-conditioned force and proprioception signatures."""
from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any, Protocol, cast

from commontrace import _jsonl, hierarchical, lineage
from commontrace import frontier_protocol as fp

JOURNAL = "robot-proprioceptive-profiles.jsonl"
ANOMALIES = "robot-proprioceptive-anomalies.jsonl"
SENSORS = {"joint_current", "force_torque", "tactile", "temperature"}


class QuantileForecaster(Protocol):
    def predict(self, history: Sequence[dict[str, Any]], covariates: dict[str, str],
                quantiles: Sequence[float]) -> dict[str, list[float]]: ...


_FORECASTERS: dict[str, QuantileForecaster] = {}


def register_forecaster(name: str, adapter: QuantileForecaster) -> None:
    fp.text(name, "forecast adapter", 64)
    if not callable(getattr(adapter, "predict", None)):
        raise TypeError("forecast adapter must implement predict")
    _FORECASTERS[name] = adapter


def _identity(value: str, label: str) -> str:
    value = fp.text(value, label, 128)
    if any(ch in value for ch in "\r\n\x00"):
        raise ValueError(f"{label} contains a control character")
    return value


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def _channels(samples: Sequence[dict[str, Any]]) -> dict[str, list[float]]:
    values: dict[str, list[float]] = defaultdict(list)
    for row in samples:
        signals = row.get("signals")
        if not isinstance(signals, dict) or not set(signals) <= SENSORS or not signals:
            raise ValueError("samples require force, tactile, temperature or joint-current signals")
        for name, raw in signals.items():
            if isinstance(raw, list):
                fp.count(len(raw), 1, 256, "downsampled sensor vector")
                for index, item in enumerate(raw):
                    values[f"{name}[{index}]"].append(fp.number(item, -1e9, 1e9, name))
            else:
                values[name].append(fp.number(raw, -1e9, 1e9, name))
    return dict(values)


def build(root: str, samples: Sequence[dict[str, Any]], *, skill: str, embodiment: str, site: str,
          alpha: float = .01, minimum_samples: int = 30) -> dict[str, Any]:
    """Build independent robust quantile bands per task phase, payload and firmware."""
    fp.count(len(samples), 1, 100_000)
    skill, embodiment, site = (_identity(v, n) for v, n in
                               ((skill, "skill"), (embodiment, "embodiment"), (site, "site")))
    fp.number(alpha, .001, .25)
    fp.count(minimum_samples, 10, 10_000)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    firmware_first_seen: dict[str, str] = {}
    for row in samples:
        fp.moment(row["timestamp"])
        _identity(row["episode_id"], "episode id")
        phase = _identity(row["phase"], "task phase")
        payload = _identity(row["payload"], "payload class")
        firmware = _identity(row["firmware"], "firmware version")
        groups[(phase, payload, firmware)].append(row)
        firmware_first_seen[firmware] = min(firmware_first_seen.get(firmware, row["timestamp"]), row["timestamp"])
    profiles = []
    for (phase, payload, firmware), rows in sorted(groups.items()):
        fp.count(len(rows), minimum_samples, 100_000, "profile samples per phase/payload/firmware")
        channels = _channels(rows)
        bands = {}
        for channel, data in channels.items():
            median = statistics.median(data)
            mad = statistics.median(abs(value - median) for value in data)
            scale = 1.4826 * mad
            residuals = [abs(value - median) / scale if scale else abs(value - median) for value in data]
            bands[channel] = {"median": median, "mad": mad, "scale": scale,
                              "cutoff": _quantile(residuals, 1 - alpha), "n": len(data)}
        profiles.append({"phase": phase, "payload": payload, "firmware": firmware,
                         "valid_from": min(row["timestamp"] for row in rows), "bands": bands})
    body = {"skill": skill, "embodiment": embodiment, "site": site, "alpha": alpha,
            "minimum_samples": minimum_samples, "firmware_order": sorted(firmware_first_seen,
                key=lambda version: (firmware_first_seen[version], version)), "profiles": profiles,
            "kind": "empirical robust quantile bands; per-channel false-alarm target is estimated"}
    digest = fp.digest(body)
    with _jsonl.locked(fp.filename(root, JOURNAL) + ".write"):
        signed = fp.append(root, JOURNAL, {"id": digest, **body})
    return cast(dict[str, Any], signed["record"])


def _latest(root: str, skill: str, embodiment: str, site: str) -> dict[str, Any]:
    rows = [row for row in fp.journal(root, JOURNAL) if row.get("skill") == skill
            and row.get("embodiment") == embodiment and row.get("site") == site]
    if not rows:
        raise KeyError("no proprioceptive signature profile is registered for this robot/task/site")
    return rows[-1]


def check(root: str, sample: dict[str, Any], *, skill: str, embodiment: str, site: str,
          scopes: Sequence[str] = (), link_recovery: bool = True,
          record_anomaly: bool = True) -> dict[str, Any]:
    profile = _latest(root, _identity(skill, "skill"), _identity(embodiment, "embodiment"), _identity(site, "site"))
    key = (_identity(sample["phase"], "task phase"), _identity(sample["payload"], "payload class"),
           _identity(sample["firmware"], "firmware version"))
    matched = [row for row in profile["profiles"]
               if (row["phase"], row["payload"], row["firmware"]) == key]
    if not matched:
        raise ValueError("sensor sample has no phase/payload/firmware baseline; fail closed for this diagnostic")
    bands = matched[-1]["bands"]
    signals = _channels([sample])
    anomalies = []
    for channel, values in signals.items():
        band = bands.get(channel)
        if band is None:
            raise ValueError("sensor channel is not present in the baseline profile")
        for index, value in enumerate(values):
            deviation = abs(value - band["median"])
            score = deviation / band["scale"] if band["scale"] else deviation
            if score > band["cutoff"]:
                anomalies.append({"channel": channel, "value": value, "median": band["median"],
                                  "score": score, "cutoff": band["cutoff"]})
    result = {"anomaly": bool(anomalies), "anomalies": anomalies, "episode_id": sample["episode_id"],
              "event_time": fp.moment(sample["timestamp"]), "recorded_at": fp.now(),
              "profile_id": profile["id"], "kind": "empirical deviation detector; not a safety function"}
    if anomalies and record_anomaly:
        identity = "signature-" + fp.digest([result, skill, embodiment, site])[:32]
        scopes = sorted(set(scopes) | {"skill:" + skill, "embodiment:" + embodiment, "site:" + site})
        text = (f"Proprioceptive anomaly during {skill} on {embodiment} at {site}: "
                + ", ".join(f"{item['channel']} deviation {item['score']:.3g} "
                             f"exceeded empirical band {item['cutoff']:.3g}" for item in anomalies))
        fact, _ = hierarchical.add_fact(root, text, scopes=scopes, valid_from=result["event_time"])
        evidence = {"id": identity, "fact_id": fact.id, "episode_id": sample["episode_id"],
                    "profile_id": profile["id"], "anomalies": anomalies, "event_time": result["event_time"]}
        lineage.attest(root, evidence, fp.sign(root, evidence), inputs=[sample["episode_id"]],
                       operation="robot-proprioceptive-anomaly")
        event = fp.append(root, ANOMALIES, {**result, "id": identity, "fact_id": fact.id,
                                           "skill": skill, "embodiment": embodiment, "site": site})
        result["memory_id"] = fact.id
        if link_recovery:
            from commontrace import recovery

            recovery_entry = recovery.register(root, "proprioceptive-signature-anomaly",
                "Pause deployment and request a qualified inspection before resuming the affected skill.",
                context={"site": site, "robot": fp.text(sample.get("robot", embodiment)),
                         "embodiment": embodiment, "environment": "real"},
                source_episodes=[sample["episode_id"]])
            result["recovery_id"] = recovery_entry["record"]["id"]
        result["event_sequence"] = event["record"]["sequence"]
    return result


def drift(root: str, *, skill: str, embodiment: str, site: str, sensor: str,
          since_firmware: str, minimum_shift: float = 1.0) -> list[dict[str, Any]]:
    sensor = fp.text(sensor, "sensor channel", 128)
    profile = _latest(root, _identity(skill, "skill"), _identity(embodiment, "embodiment"),
                      _identity(site, "site"))
    order = profile["firmware_order"]
    if since_firmware not in order:
        raise ValueError("requested firmware boundary is not in the profile history")
    index = order.index(since_firmware)
    groups: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in profile["profiles"]:
        band = row["bands"].get(sensor)
        if band is not None:
            groups[(row["phase"], row["payload"])][row["firmware"]] = band
    results = []
    for (phase, payload), versions in sorted(groups.items()):
        baseline = versions.get(since_firmware)
        if baseline is None:
            continue
        for firmware in order[index + 1:]:
            current = versions.get(firmware)
            if current is None:
                continue
            shift = current["median"] - baseline["median"]
            if abs(shift) >= fp.number(minimum_shift, 0, 1e9):
                results.append({"skill": skill, "embodiment": embodiment, "site": site, "sensor": sensor,
                    "phase": phase, "payload": payload, "since_firmware": since_firmware,
                    "firmware": firmware, "baseline_median": baseline["median"],
                    "current_median": current["median"], "shift": shift,
                    "valid_from": next(row["valid_from"] for row in profile["profiles"]
                                        if row["phase"] == phase and row["payload"] == payload
                                        and row["firmware"] == firmware), "recorded_at": fp.now(),
                    "kind": "bitemporal maintenance estimate; requires engineering review"})
    return results


def ingest(root: str, records: Iterable[dict[str, Any]], *, skill: str, embodiment: str, site: str) -> int:
    """Ingest bounded sensor samples from ROS/MCAP record callbacks."""
    total = 0
    for index, sample in enumerate(records):
        fp.count(index, 0, 99_999, "robot signature sample count")
        signals = sample.get("proprioception")
        if signals is None:
            continue
        if not isinstance(signals, dict):
            raise ValueError("proprioception field must contain sensor channels")
        row = {"episode_id": sample["occasion_id"], "timestamp": sample["timestamp"],
               "phase": sample["phase"], "payload": sample["payload"], "firmware": sample["firmware"],
               "signals": signals}
        _channels([row])
        fp.append(root, "robot-proprioceptive-samples.jsonl", row)
        total += 1
    return total


def forecast(history: Sequence[dict[str, Any]], *, adapter: str, covariates: dict[str, str],
             quantiles: Sequence[float] = (.1, .5, .9)) -> dict[str, list[float]]:
    """Optional pluggable quantile forecast; published Chronos work does not establish robotics validity."""
    if adapter not in _FORECASTERS:
        raise RuntimeError("forecast extra adapter is not installed")
    fp.count(len(history), 1, 10_000)
    levels = [fp.number(q, .001, .999) for q in quantiles]
    if levels != sorted(set(levels)):
        raise ValueError("forecast quantile levels must be unique and increasing")
    result = _FORECASTERS[adapter].predict(history, covariates, levels)
    fp.wire(result)
    return result


def evaluate_latency(samples: Sequence[dict[str, Any]], *, budget_ms: float = 20) -> dict[str, Any]:
    """Measure bounded edge-side sample normalization cost without network I/O."""
    fp.count(len(samples), 1, 1000)
    budget_ms = fp.number(budget_ms, .1, 1000)
    started = time.perf_counter()
    for sample in samples:
        _channels([sample])
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {"samples": len(samples), "elapsed_ms": elapsed_ms, "budget_ms": budget_ms,
            "within_budget": elapsed_ms <= budget_ms,
            "kind": "local normalization measurement; host-specific latency estimate"}
