"""Many robots, one experiment."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field

from commontrace import gateway, holdout_io, paths

LOG_FILES = (holdout_io.holdout_log_path, holdout_io.outcomes_log_path)


class FleetError(ValueError):
    """The stores cannot be pooled as asked."""


@dataclass
class StoreSummary:
    path: str
    salt: str
    rate: float
    env: str | None
    protected: tuple
    assignments: int
    outcomes: int
    corrupt: int
    started: bool


@dataclass
class CheckReport:
    stores: list[StoreSummary] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    duplicates: int = 0

    @property
    def mergeable(self) -> bool:
        return not self.problems and not self.conflicts


def _read_jsonl(path: str) -> tuple[list[dict], int]:
    rows, corrupt = [], 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    corrupt += 1
                    continue
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    corrupt += 1
    except OSError:
        pass
    return rows, corrupt


def summarise(root: str) -> StoreSummary:
    config = holdout_io.load_config(root)
    gw = gateway.load_config(root)
    assignments, bad_a = _read_jsonl(holdout_io.holdout_log_path(root))
    outcomes, bad_o = _read_jsonl(holdout_io.outcomes_log_path(root))
    return StoreSummary(
        path=root, salt=config.salt, rate=config.rate, env=gw.env, protected=gw.protected_prefixes,
        assignments=len(assignments), outcomes=len(outcomes), corrupt=bad_a + bad_o,
        started=bool(config.started_at))


def _assignment_key(row: dict) -> tuple:
    return (str(row.get("lesson", "")), str(row.get("occasion_id", "")))


def check(sources: list[str]) -> CheckReport:
    """Whether `sources` can be merged, and everything that stops them."""
    if len(sources) < 2:
        raise FleetError("name at least two stores to merge")
    report = CheckReport(stores=[summarise(s) for s in sources])
    first = report.stores[0]
    for other in report.stores[1:]:
        if not other.started or not first.started:
            report.problems.append(
                f"{other.path if not other.started else first.path} has no experiment started: "
                "there is no randomization to pool")
            continue
        if other.salt != first.salt:
            report.problems.append(
                f"{other.path} ran a different randomization (salt {other.salt!r} vs {first.salt!r}); "
                "adding its log to the others' would pool two experiments. Give every robot the "
                "same one with `commontrace fleet adopt`.")
        if abs(other.rate - first.rate) > 1e-9:
            report.problems.append(
                f"{other.path} held out {other.rate:.0%}, {first.path} {first.rate:.0%}: not one design")
        if other.env != first.env:
            report.problems.append(
                f"{other.path} measures {other.env!r} and {first.path} {first.env!r}; simulation and "
                "reality are never pooled")
    arms: dict[tuple, tuple[bool, str]] = {}
    answers: dict[str, tuple[bool, str]] = {}
    seen_lines: set[str] = set()
    for store in report.stores:
        for row in _read_jsonl(holdout_io.holdout_log_path(store.path))[0]:
            canon = json.dumps(row, sort_keys=True)
            if canon in seen_lines:
                report.duplicates += 1
                continue
            seen_lines.add(canon)
            key, injected = _assignment_key(row), bool(row.get("injected"))
            prior = arms.get(key)
            if prior is not None and prior[0] != injected:
                report.conflicts.append(
                    f"memory {key[0]!r} on occasion {key[1]!r} is in opposite arms in {prior[1]} and "
                    f"{store.path}")
            else:
                arms.setdefault(key, (injected, store.path))
        for row in _read_jsonl(holdout_io.outcomes_log_path(store.path))[0]:
            canon = json.dumps(row, sort_keys=True)
            if canon in seen_lines:
                report.duplicates += 1
                continue
            seen_lines.add(canon)
            occasion, succeeded = row.get("occasion_id"), row.get("succeeded")
            if not isinstance(occasion, str) or not isinstance(succeeded, bool):
                continue
            prior = answers.get(occasion)
            if prior is not None and prior[0] != succeeded:
                report.conflicts.append(
                    f"occasion {occasion!r} succeeded in {prior[1]} and failed in {store.path}"
                    if prior[0] else
                    f"occasion {occasion!r} failed in {prior[1]} and succeeded in {store.path}")
            else:
                answers.setdefault(occasion, (succeeded, store.path))
    return report


def adopt(source: str, dest: str) -> dict:
    config = holdout_io.load_config(source)
    if not config.started_at:
        raise FleetError(f"{source} has no experiment started; start one first "
                         "(`commontrace proof start` or `commontrace experiment --configure`)")
    log = holdout_io.holdout_log_path(dest)
    if os.path.isfile(log) and os.path.getsize(log) > 0:
        raise FleetError(f"{dest} already holds assignments; adopting a different randomization "
                         "under them would pool two experiments")
    os.makedirs(paths.memory_dir(dest), exist_ok=True)
    shutil.copyfile(holdout_io.config_path(source), holdout_io.config_path(dest))
    gw = gateway.load_config(source)
    gateway.save_config(dest, gw)
    return {"salt": config.salt, "rate": config.rate, "env": gw.env,
            "protected_prefixes": list(gw.protected_prefixes)}


def _time_key(row: dict) -> str:
    return str(row.get("at") or "")


def merge(sources: list[str], dest: str, *, report: CheckReport | None = None) -> dict:
    """Write one store holding every source's records. Sources are not modified."""
    report = report or check(sources)
    if not report.mergeable:
        raise FleetError("cannot merge: " + "; ".join([*report.problems, *report.conflicts][:5]))
    log = holdout_io.holdout_log_path(dest)
    if os.path.isfile(log) and os.path.getsize(log) > 0:
        raise FleetError(f"{dest} already holds assignments; merge into an empty store")
    os.makedirs(paths.memory_dir(dest), exist_ok=True)

    totals = {"assignments": 0, "outcomes": 0, "duplicates": report.duplicates, "events": 0}
    for name, path_of in (("assignments", holdout_io.holdout_log_path),
                          ("outcomes", holdout_io.outcomes_log_path)):
        merged, seen = [], set()
        for src in sources:
            for row in _read_jsonl(path_of(src))[0]:
                canon = json.dumps(row, sort_keys=True)
                if canon not in seen:
                    seen.add(canon)
                    merged.append(row)
        merged.sort(key=_time_key)
        with open(path_of(dest), "w", encoding="utf-8", newline="\n") as fh:
            for row in merged:
                fh.write(json.dumps(row) + "\n")
        totals[name] = len(merged)

    events_path = os.path.join(paths.memory_dir(dest), gateway.EVENTS_NAME)
    events, seen = [], set()
    for src in sources:
        for row in _read_jsonl(os.path.join(paths.memory_dir(src), gateway.EVENTS_NAME))[0]:
            canon = json.dumps(row, sort_keys=True)
            if canon not in seen:
                seen.add(canon)
                events.append(row)
    if events:
        events.sort(key=_time_key)
        with open(events_path, "w", encoding="utf-8", newline="\n") as fh:
            for row in events:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    totals["events"] = len(events)

    shutil.copyfile(holdout_io.config_path(sources[0]), holdout_io.config_path(dest))
    gateway.save_config(dest, gateway.load_config(sources[0]))
    for src in sources:
        proof_state = os.path.join(paths.memory_dir(src), "proof.json")
        if os.path.isfile(proof_state) and not os.path.isfile(os.path.join(paths.memory_dir(dest), "proof.json")):
            shutil.copyfile(proof_state, os.path.join(paths.memory_dir(dest), "proof.json"))
    return totals
