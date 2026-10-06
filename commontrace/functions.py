"""Function kits: what makes CommonTrace work for any business function."""
from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass

from commontrace import experiment, holdout_io, outcome_detect, paths

DEMO_NOTE = "SYNTHETIC DEMO DATA"

COMBINE_RULES = ("single", "all", "any")

_TOP_KEYS = {"key", "title", "agent_type", "occasion", "outcome", "planning", "domains",
             "packs", "regulated"}
_OCCASION_KEYS = {"label", "example"}
_OUTCOME_KEYS = {"success", "window_days", "signals", "combine"}
_PLANNING_KEYS = {"baseline", "effect", "holdout_rate"}


class KitError(ValueError):
    """A kit spec that cannot be used, with every problem found in it."""


@dataclass(frozen=True)
class Outcome:
    success: str
    window_days: float
    signals: tuple[str, ...]
    combine: str


@dataclass(frozen=True)
class Planning:
    baseline: float
    effect: float
    holdout_rate: float


@dataclass(frozen=True)
class FunctionKit:
    key: str
    title: str
    agent_type: str
    occasion_label: str
    occasion_example: str
    outcome: Outcome
    planning: Planning
    domains: tuple[str, ...] = ()
    packs: tuple[str, ...] = ()
    regulated: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key, "title": self.title, "agent_type": self.agent_type,
            "occasion": {"label": self.occasion_label, "example": self.occasion_example},
            "outcome": {
                "success": self.outcome.success, "window_days": self.outcome.window_days,
                "signals": list(self.outcome.signals), "combine": self.outcome.combine,
            },
            "planning": {
                "baseline": self.planning.baseline, "effect": self.planning.effect,
                "holdout_rate": self.planning.holdout_rate,
            },
            "domains": list(self.domains), "packs": list(self.packs), "regulated": self.regulated,
        }


def detector_names() -> tuple[str, ...]:
    """The signals a kit may name: every `from_*` detector in outcome_detect."""
    return tuple(sorted(
        n for n in dir(outcome_detect)
        if n.startswith("from_") and callable(getattr(outcome_detect, n))
    ))


def _number(problems: list[str], value, name: str, low: float, high: float,
            *, low_open: bool, high_open: bool) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if ok:
        above = value > low if low_open else value >= low
        below = value < high if high_open else value <= high
        ok = above and below
    if not ok:
        problems.append(f"{name} must be a number in {'(' if low_open else '['}{low}, "
                        f"{high}{')' if high_open else ']'}, got {value!r}")
        return low
    return float(value)


def _text(problems: list[str], value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{name} must be non-empty text")
        return ""
    return value.strip()


def _slugs(problems: list[str], value, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{name} must be a list of text")
        return ()
    bad = [v for v in value if not paths.AGENT_TYPE_RE.match(v)]
    if bad:
        problems.append(f"{name} entries must be lowercase slugs, got {bad}")
    return tuple(value)


def from_dict(spec: object) -> FunctionKit:
    """Validate `spec` and build a kit, or raise KitError listing every problem."""
    if not isinstance(spec, dict):
        raise KitError("a kit must be a JSON object")
    problems: list[str] = []
    for unknown in sorted(set(spec) - _TOP_KEYS):
        problems.append(f"unknown field {unknown!r}")
    occasion = spec.get("occasion") if isinstance(spec.get("occasion"), dict) else {}
    outcome = spec.get("outcome") if isinstance(spec.get("outcome"), dict) else {}
    planning = spec.get("planning") if isinstance(spec.get("planning"), dict) else {}
    for name, section, allowed in (("occasion", occasion, _OCCASION_KEYS),
                                   ("outcome", outcome, _OUTCOME_KEYS),
                                   ("planning", planning, _PLANNING_KEYS)):
        if not section:
            problems.append(f"{name} is required")
        for unknown in sorted(set(section) - allowed):
            problems.append(f"unknown field {name}.{unknown}")

    key = spec.get("key")
    if not isinstance(key, str) or not paths.AGENT_TYPE_RE.match(key):
        problems.append(f"key must be a lowercase slug, got {key!r}")
        key = ""
    agent_type = spec.get("agent_type", key)
    if not isinstance(agent_type, str) or not paths.AGENT_TYPE_RE.match(agent_type):
        problems.append(f"agent_type must be a lowercase slug, got {agent_type!r}")
        agent_type = key

    signals = outcome.get("signals")
    names = detector_names()
    if not isinstance(signals, list) or not signals or not all(isinstance(s, str) for s in signals):
        problems.append("outcome.signals must be a non-empty list of detector names")
        signals = []
    else:
        for s in signals:
            if s not in names:
                problems.append(f"outcome.signals names {s!r}, which is not a detector "
                                f"(known: {', '.join(names)})")
    combine = outcome.get("combine", "single" if len(signals) == 1 else None)
    if combine not in COMBINE_RULES:
        problems.append(f"outcome.combine must be one of {', '.join(COMBINE_RULES)}, got {combine!r}")
    elif combine == "single" and len(signals) > 1:
        problems.append("outcome.combine 'single' takes exactly one signal; use 'all' or 'any'")
    elif combine != "single" and len(signals) == 1:
        problems.append(f"outcome.combine {combine!r} needs more than one signal")

    window = _number(problems, outcome.get("window_days"), "outcome.window_days",
                     0, 365, low_open=False, high_open=False)
    kit = FunctionKit(
        key=key,
        title=_text(problems, spec.get("title"), "title"),
        agent_type=agent_type,
        occasion_label=_text(problems, occasion.get("label"), "occasion.label"),
        occasion_example=_text(problems, occasion.get("example"), "occasion.example"),
        outcome=Outcome(
            success=_text(problems, outcome.get("success"), "outcome.success"),
            window_days=window, signals=tuple(signals), combine=str(combine),
        ),
        planning=Planning(
            baseline=_number(problems, planning.get("baseline"), "planning.baseline",
                             0, 1, low_open=True, high_open=True),
            effect=_number(problems, planning.get("effect"), "planning.effect",
                           0, 1, low_open=True, high_open=True),
            holdout_rate=_number(problems, planning.get("holdout_rate"), "planning.holdout_rate",
                                 0, 1, low_open=True, high_open=True),
        ),
        domains=_slugs(problems, spec.get("domains"), "domains"),
        packs=_slugs(problems, spec.get("packs"), "packs"),
        regulated=spec.get("regulated", False) is True,
    )
    if spec.get("regulated", False) not in (True, False):
        problems.append("regulated must be true or false")
    if problems:
        raise KitError("; ".join(problems))
    return kit


def load_file(path: str) -> FunctionKit:
    try:
        with open(path, encoding="utf-8") as fh:
            return from_dict(json.load(fh))
    except OSError as exc:
        raise KitError(f"cannot read {path}: {exc.strerror or exc}") from None
    except ValueError as exc:
        if isinstance(exc, KitError):
            raise
        raise KitError(f"{path} is not valid JSON: {exc}") from None


def builtin_kits() -> dict[str, FunctionKit]:
    from commontrace.function_kits import SPECS

    return {spec["key"]: from_dict(spec) for spec in SPECS}


def resolve(name_or_path: str) -> FunctionKit:
    """A built-in kit by key (or by its agent_type), or a kit file by path."""
    if os.path.isfile(name_or_path):
        return load_file(name_or_path)
    kits = builtin_kits()
    if name_or_path in kits:
        return kits[name_or_path]
    for kit in kits.values():
        if kit.agent_type == name_or_path:
            return kit
    raise KitError(f"no function kit {name_or_path!r}; built in: {', '.join(sorted(kits))}. "
                   "For anything else pass a kit file (see `commontrace function check`).")


def kit_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "kit.json")


def save_to_store(root: str, kit: FunctionKit) -> None:
    os.makedirs(paths.memory_dir(root), exist_ok=True)
    with open(kit_path(root), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(kit.to_dict(), fh, indent=2)
        fh.write("\n")


def store_kit(root: str) -> FunctionKit | None:
    try:
        return load_file(kit_path(root))
    except KitError:
        return None


@dataclass(frozen=True)
class Forecast:
    kit: FunctionKit
    daily_occasions: float
    baseline: float
    effect: float
    rate: float
    design: experiment.Design
    days_to_collect: int
    days_to_verdict: int
    assumed_baseline: bool
    daily_needed: int | None = None
    within_days: int | None = None


def forecast(
    kit: FunctionKit, daily_occasions: float, *, baseline: float | None = None,
    effect: float | None = None, rate: float | None = None, within_days: int | None = None,
) -> Forecast:
    """How long until this function's holdout can answer, at this volume."""
    if not isinstance(daily_occasions, (int, float)) or not daily_occasions > 0 \
            or not math.isfinite(daily_occasions):
        raise ValueError("daily_occasions must be a positive number")
    b = kit.planning.baseline if baseline is None else baseline
    e = kit.planning.effect if effect is None else effect
    r = kit.planning.holdout_rate if rate is None else rate
    design = experiment.plan(effect=e, baseline=b, rate=r)
    collect = math.ceil(design.occasions_needed / daily_occasions)
    needed = None
    if within_days is not None:
        room = within_days - kit.outcome.window_days
        if room > 0:
            needed = math.ceil(design.occasions_needed / room)
    return Forecast(
        kit=kit, daily_occasions=float(daily_occasions), baseline=b, effect=e, rate=r,
        design=design, days_to_collect=collect,
        days_to_verdict=collect + math.ceil(kit.outcome.window_days),
        assumed_baseline=baseline is None, daily_needed=needed, within_days=within_days,
    )


def render_forecast(f: Forecast) -> str:
    kit = f.kit
    lines = [
        f"# {kit.title}: time to a verdict",
        "",
        f"At **{f.daily_occasions:g} {kit.occasion_label}s a day**, a {f.rate:.0%} holdout and a "
        f"smallest worthwhile effect of **{f.effect:.0%}** against a **{f.baseline:.0%}** baseline:",
        "",
        f"- {f.design.occasions_needed:,} occasions are needed ({f.design.n_per_arm:,} in each arm).",
        f"- ~{f.days_to_collect} days to collect them, then up to {kit.outcome.window_days:g} days "
        f"for the last outcomes to mature: **a verdict in ~{f.days_to_verdict} days.**",
        "",
        f"Success means: {kit.outcome.success}.",
    ]
    if f.within_days is not None:
        if f.daily_needed is None:
            lines += ["", f"**No volume delivers a verdict within {f.within_days} days:** the "
                      f"{kit.outcome.window_days:g}-day outcome window alone uses it up."]
        else:
            lines += ["", f"To have a verdict within {f.within_days} days you need "
                      f"**~{f.daily_needed:,} {kit.occasion_label}s a day**."]
    if f.assumed_baseline:
        lines += ["", f"The {f.baseline:.0%} baseline is a planning assumption, not a measurement. "
                  "Pass `--baseline` with your own current success rate: nearer 50% needs more "
                  "occasions, nearer 0% or 100% fewer."]
    if f.days_to_verdict > 120:
        lines += ["", "**This volume is too low to answer in a quarter.** Accept a larger effect "
                  "as the thing being tested for (`--effect`), or measure more agents together."]
    return "\n".join(lines)


def render_kit(kit: FunctionKit) -> str:
    out = kit.outcome
    rule = "" if out.combine == "single" else f" combined with '{out.combine}'"
    lines = [
        f"# {kit.title}  (`{kit.key}`, agent_type `{kit.agent_type}`)",
        "",
        f"- **Occasion:** one {kit.occasion_label} (e.g. `{kit.occasion_example}`). "
        "Use the same id to retrieve and to report the outcome.",
        f"- **Success:** {out.success}.",
        f"- **Wait:** {out.window_days:g} days before an outcome is final.",
        f"- **Detectors:** {', '.join(out.signals)}{rule} (`commontrace.outcome_detect`).",
        f"- **Planning assumptions** (replace with your own): baseline {kit.planning.baseline:.0%}, "
        f"smallest effect worth detecting {kit.planning.effect:.0%}, holdout "
        f"{kit.planning.holdout_rate:.0%}.",
    ]
    if kit.domains:
        lines.append(f"- **Starter domains:** {', '.join(kit.domains)}.")
    if kit.regulated:
        lines += ["", "Regulated function: the outcome is a human reviewer's acceptance of the "
                  "agent's work. CommonTrace measures whether a memory changes that, not "
                  "whether the underlying decision was correct."]
    return "\n".join(lines)


# Demo effects are deliberately well above the practical threshold so the
# synthetic proof remains decisive under the anytime-valid sequential boundary.
DEMO_LESSONS = (
    ("demo-helpful-memory", +0.25),
    ("demo-harmful-memory", -0.25),
    ("demo-neutral-memory", 0.0),
)
DEMO_OCCASIONS = 2400


def is_demo_store(root: str) -> bool:
    return holdout_io.load_config(root).note.startswith(DEMO_NOTE)


def seed_demo(root: str, kit: FunctionKit, *, seed: int = 0,
              occasions: int = DEMO_OCCASIONS) -> list[str]:
    """Fill `root` with synthetic, labelled holdout data in this kit's terms."""
    existing = holdout_io.holdout_log_path(root)
    if os.path.isfile(existing) and os.path.getsize(existing) > 0 and not is_demo_store(root):
        raise KitError(f"{root} already holds real holdout data; demo data goes in an empty store")
    config = holdout_io.configure(
        root, rate=kit.planning.holdout_rate, detect=kit.planning.effect,
        note=f"{DEMO_NOTE} ({kit.key}); not a customer measurement",
        salt=f"demo-{kit.key}-{seed}",
    )
    rng = random.Random(f"{kit.key}:{seed}")
    slugs = [s for s, _ in DEMO_LESSONS]
    effects = dict(DEMO_LESSONS)
    revisions = {slug: f"demo-{slug}-r1" for slug in slugs}
    for i in range(occasions):
        slug = slugs[i % len(slugs)]
        withheld = holdout_io.assign_and_log(
            root, [slug], occasion_id=f"DEMO-{kit.key}-{i:05d}", rate=kit.planning.holdout_rate,
            salt=config.salt, revisions={slug: revisions[slug]},
        )
        p = kit.planning.baseline + (effects[slug] if slug not in withheld else 0.0)
        holdout_io.record_outcome(root, f"DEMO-{kit.key}-{i:05d}", rng.random() < min(max(p, 0.02), 0.98))
    return slugs
