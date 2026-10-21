"""Installable, offline vertical configuration bundles and seeded reference demos."""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unimem import frontier_protocol as fp

PACK_DIR = Path(__file__).with_name("vertical_packs")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


@dataclass(frozen=True)
class PackInfo:
    name: str
    version: str
    features: tuple[str, ...]


def _load(name: str) -> tuple[dict[str, Any], str]:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError("invalid vertical pack name")
    path = PACK_DIR / f"{name}.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise KeyError(f"unknown vertical pack: {name}") from None
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("name") != name:
        raise ValueError("vertical pack manifest identity mismatch")
    return value, hashlib.sha256(raw).hexdigest()


def list_packs() -> list[PackInfo]:
    output = []
    for path in sorted(PACK_DIR.glob("*.json")):
        data, _ = _load(path.stem)
        output.append(PackInfo(path.stem, data["version"], tuple(data["features"])))
    return output


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".pack-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


def install(name: str, destination: str) -> dict[str, Any]:
    manifest, digest = _load(name)
    docs = PACK_DIR / f"{name}.md"
    if not docs.is_file():
        raise ValueError("vertical pack is missing its documentation page")
    root = Path(destination).expanduser().resolve()
    target = root / name
    if target.is_symlink():
        raise ValueError("vertical pack destination must not be a symlink")
    if target.exists() and not target.is_dir():
        raise ValueError("vertical pack destination must be a directory")
    _atomic_write(target / "pack.json", json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n")
    _atomic_write(target / "README.md", docs.read_bytes())
    return {"name": name, "version": manifest["version"], "sha256": digest,
            "installed_to": str(target), "network_calls": 0}


def demo(name: str, *, seed: int = 20261018) -> dict[str, Any]:
    from unimem import pack_demos

    manifest, _ = _load(name)
    seed = fp.count(seed, 0, 2**32 - 1)
    checks = pack_demos.execute(manifest["features"], seed=seed)
    config = manifest["demo"]
    n = fp.count(config["units_per_arm"], 20, 100_000)
    p0 = fp.number(config["baseline_success_probability"], 0.001, 0.999)
    p1 = fp.number(config["treatment_success_probability"], 0.001, 0.999)
    harm_p = fp.number(config["harm_probability"], 0, 1)
    rng = random.Random(seed)
    control = sum(rng.random() < p0 for _ in range(n))
    treatment = sum(rng.random() < p1 for _ in range(n))
    harms = sum(rng.random() < harm_p for _ in range(n))
    rate0, rate1 = control / n, treatment / n
    effect = rate1 - rate0
    se = math.sqrt(rate0 * (1 - rate0) / n + rate1 * (1 - rate1) / n)
    lower, upper = effect - 1.96 * se, effect + 1.96 * se
    harm_budget = 0.05
    spent = (harms / n) / harm_budget
    report = manifest["report_template"].format(name=name, seed=seed, n=2 * n, effect=effect,
        lower=lower, upper=upper, harm=spent, features=", ".join(manifest["features"]))
    report += "\nOffline feature checks (local fixtures, separate from synthetic estimates):\n"
    report += "\n".join(f"- {feature}: {check['detail']}" for feature, check in checks.items())
    return {"name": name, "seed": seed, "units_per_arm": n, "control_successes": control,
            "treatment_successes": treatment, "harm_events": harms, "effect": effect,
            "interval_95": [lower, upper], "harm_budget_spent": spent, "report": report,
            "network_calls": 0, "synthetic": True, "feature_checks": checks}
