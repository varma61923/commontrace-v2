"""The Agent Learning Proof: from "will this answer in time?" to a package a
third party can check without trusting whoever produced it.

    start    forecast (refuse a run that cannot answer), start the randomized
             holdout, and register what is being measured BEFORE any data exists
    status   how far along, whether the run can be trusted, what each memory shows
    report   a package: a readable report, a machine-readable record, the raw
             assignment rows, and -- with a key -- an issuer signature over all three
    verify   recompute everything from the raw rows and say what does not match

Nothing here is a new statistic. It composes what already exists -- the
preregistration (commontrace/prereg.py), the validity audit
(commontrace/integrity.py), the estimator (commontrace/experiment.py), the value
ledger and its signature (commontrace/value.py), the raw export
(commontrace/raw_export.py) -- so the package cannot say anything the
`commontrace experiment` report would not.

WHAT MAKES IT A PROOF AND NOT A PRESENTATION
--------------------------------------------
* The design is fixed first. `start` writes the outcome, the smallest effect that
  matters, the holdout rate and the stopping rule, with the salt that names the
  randomization, and the fingerprint of that goes under the signature.
* A compromised experiment states no figure; an unfinished one says so in its
  headline; a harmful memory is reported with the benefit, not instead of it.
* `verify` does not read the report's numbers and compare them to themselves. It
  re-runs the audit, the estimates and the value arithmetic from
  `assignments.csv` alone, so an auditor needs the CSV and a key, not trust.
  The data digest, the ledger chain, the preregistration fingerprint and the
  signature are checked as well.

WHAT IT CANNOT SHOW (and says so in every report)
-------------------------------------------------
That the agent honoured a withheld memory (that leaves no trace and biases the
result toward zero), that outcomes were reported honestly, or that the
registration predates the data beyond what the assignment log's own timestamps
say. A signature authenticates who issued the package, not that the issuer was
honest about what it ran. Synthetic demo data is marked as such everywhere it
appears.
"""
from __future__ import annotations

import csv
import dataclasses
import datetime
import io
import json
import os
from dataclasses import dataclass, field

from commontrace import (
    experiment,
    functions,
    holdout_io,
    integrity,
    paths,
    prereg,
    raw_export,
    retrieval_io,
    value,
)

SCHEMA = 1
STATE_NAME = "proof.json"
MARKDOWN_NAME, RECORD_NAME, DATA_NAME, PAGE_NAME = "report.md", "proof.json", "assignments.csv", "index.html"
#: A run that cannot give a verdict within this many days at the stated volume is
#: refused at `start` unless forced: the failure it prevents is a spent pilot.
MAX_DAYS = 120
MIN_KEY_BYTES = 16

PASS, FAIL, SKIP, UNSIGNED = "PASS", "FAIL", "SKIP", "UNSIGNED"


class ProofError(ValueError):
    """The proof cannot be started, built or read as asked."""


def state_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), STATE_NAME)


def load_state(root: str) -> dict | None:
    try:
        with open(state_path(root), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None


def _save_state(root: str, state: dict) -> None:
    os.makedirs(paths.memory_dir(root), exist_ok=True)
    tmp = state_path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, state_path(root))


def _now(now: datetime.datetime | None) -> datetime.datetime:
    return now or datetime.datetime.now(datetime.timezone.utc)


# --- Start --------------------------------------------------------------------------


def _design(kit: functions.FunctionKit, daily: float, baseline, effect, rate):
    return functions.forecast(kit, daily, baseline=baseline, effect=effect, rate=rate)


def _state_for(kit, label, fc, vpo, registration, *, synthetic, now) -> dict:
    return {
        "schema": SCHEMA, "label": label, "kit": kit.to_dict(),
        "baseline": fc.baseline, "effect": fc.effect, "rate": fc.rate,
        "daily_occasions": fc.daily_occasions,
        "planned_occasions": fc.design.occasions_needed,
        "forecast_days_to_verdict": fc.days_to_verdict,
        "value_per_occasion": vpo,
        "started_at": _now(now).isoformat(),
        "stopping_rule": registration.stopping_rule,
        "preregistration": registration.to_dict(),
        "synthetic": synthetic,
    }


def start(
    root: str, kit: functions.FunctionKit, *, label: str, daily: float,
    value_per_occasion: float | None = None, baseline: float | None = None,
    effect: float | None = None, rate: float | None = None, max_days: int = MAX_DAYS,
    force: bool = False, now: datetime.datetime | None = None,
) -> tuple[dict, functions.Forecast]:
    """Forecast, refuse what cannot answer, start the holdout, register the design."""
    label = (label or "").strip()
    if not label:
        raise ProofError("a label is required, e.g. the customer or the agent fleet")
    if value_per_occasion is not None and not value_per_occasion > 0:
        raise ProofError("value_per_occasion must be a positive number")
    existing = load_state(root)
    if existing is not None and not force:
        raise ProofError(
            f"this store already has a proof in progress ({existing['label']!r}, started "
            f"{existing['started_at'][:10]}). Report it, or pass --force to start a new one "
            "(which begins a fresh randomization; earlier assignments are not pooled in).")
    try:
        fc = _design(kit, daily, baseline, effect, rate)
    except ValueError as exc:
        raise ProofError(str(exc)) from None
    if fc.days_to_verdict > max_days and not force:
        raise ProofError(
            f"at {daily:g} {kit.occasion_label}s a day a verdict takes ~{fc.days_to_verdict} days, "
            f"beyond {max_days}. A run that cannot answer in time is a spent pilot.\n\n"
            + functions.render_forecast(fc)
            + "\n\nRaise the volume, accept a larger effect (--effect), or pass --force.")
    config = holdout_io.configure(
        root, rate=fc.rate, detect=fc.effect, note=f"Agent Learning Proof: {label}")
    registration = prereg.register(
        primary_outcome=kit.outcome.success, minimum_practical_effect=fc.effect,
        holdout_rate=fc.rate, planned_occasions=fc.design.occasions_needed,
        stopping_rule=prereg.STOP_SEQUENTIAL, salt=config.salt,
        notes=f"{kit.title}; occasion = one {kit.occasion_label}; window {kit.outcome.window_days:g}d",
        now=now)
    state = _state_for(kit, label, fc, value_per_occasion, registration, synthetic=False, now=now)
    _save_state(root, state)
    return state, fc


def start_demo(
    root: str, kit: functions.FunctionKit, *, value_per_occasion: float | None = None,
    seed: int = 0, now: datetime.datetime | None = None,
) -> dict:
    """A proof on synthetic data, so the report can be shown before there is any.

    The design is registered BEFORE the data is written (the salt is
    deterministic, so it is known in advance), exactly as a real run must be, so
    the demo exercises the pre-registration check honestly. Everything it
    produces carries `synthetic: true` and a banner.
    """
    moment = _now(now)
    fc = functions.forecast(kit, max(1.0, functions.DEMO_OCCASIONS / 30))
    registration = prereg.register(
        primary_outcome=kit.outcome.success, minimum_practical_effect=kit.planning.effect,
        holdout_rate=kit.planning.holdout_rate, planned_occasions=fc.design.occasions_needed,
        stopping_rule=prereg.STOP_SEQUENTIAL, salt=f"demo-{kit.key}-{seed}",
        notes=f"{functions.DEMO_NOTE}: not a customer measurement", now=moment)
    functions.seed_demo(root, kit, seed=seed)
    state = _state_for(kit, f"demo-{kit.key}", fc, value_per_occasion, registration,
                       synthetic=True, now=moment)
    _save_state(root, state)
    return state


SIM_MEMORIES = (("sim-helpful-memory", +1), ("sim-harmful-memory", -1), ("sim-neutral-memory", 0))
#: Each simulated memory is eligible on one occasion in three, so a rehearsal runs this many
#: times the planned occasions: enough for the sequential interval to rule an effect out.
SIM_PLAN_MULTIPLE = 5


def simulate_fleet(root: str, kit: functions.FunctionKit, *, seed: int = 0,
                   occasions: int | None = None, planted: float | None = None) -> dict:
    """Drive a simulated fleet through the real path of a proof that was just started.

    The same calls a customer's agent makes (`CausalMemory.recall`, `record_outcome`) against
    the store `start` configured, with outcomes drawn HERE from planted truth: one memory
    that raises the success rate, one that lowers it, one that does nothing, each eligible on
    a third of occasions. Used to show the whole wizard end to end, and by the test that a
    simulated fleet reaches the right verdicts. Refuses a store that already has assignments.
    """
    import random

    from commontrace.measure import CausalMemory

    log = holdout_io.holdout_log_path(root)
    if os.path.isfile(log) and os.path.getsize(log) > 0:
        raise ProofError("simulation needs a fresh store: it would otherwise mix synthetic occasions "
                         "into a real log")
    rng = random.Random(f"sim:{kit.key}:{seed}")
    texts = {slug: f"{slug} (simulation)" for slug, _ in SIM_MEMORIES}
    truth = dict(SIM_MEMORIES)
    current = {"slug": SIM_MEMORIES[0][0]}
    memory = CausalMemory(lambda q, **kw: [{"id": current["slug"], "memory": texts[current["slug"]]}],
                          root=root, on_harm="inform")
    state = load_state(root)
    baseline = state["baseline"]
    occasions = occasions or int(state["planned_occasions"]) * SIM_PLAN_MULTIPLE
    # Larger than the design's smallest worthwhile effect, so the rehearsal is powered to decide.
    planted = planted if planted is not None else max(0.15, 1.5 * float(state["effect"]))
    for i in range(occasions):
        current["slug"] = SIM_MEMORIES[i % len(SIM_MEMORIES)][0]
        delivered = {item["id"] for item in memory.recall("simulated task", occasion_id=f"SIM-{i:05d}")}
        p = baseline + (truth[current["slug"]] * planted if current["slug"] in delivered else 0.0)
        memory.record_outcome(f"SIM-{i:05d}", succeeded=rng.random() < min(max(p, 0.02), 0.98))
    return {"occasions": occasions, "planted": {slug: truth[slug] * planted for slug in truth}}


def verdict_matches(planted: float, verdict: str | None) -> bool:
    """Whether `verdict` is a correct reading of a planted effect. A memory that does nothing is
    read correctly unless it is CLAIMED to help or hurt: whether the interval is already narrow
    enough to rule an effect out is a matter of sample size, not correctness."""
    if planted > 0:
        return verdict == experiment.VERDICT_HELPS
    if planted < 0:
        return verdict == experiment.VERDICT_HURTS
    return verdict not in (None, experiment.VERDICT_HELPS, experiment.VERDICT_HURTS)


# --- Analysis (one place, used by status, report and verify) --------------------------


@dataclass
class Analysis:
    rows: list
    report: integrity.IntegrityReport
    effects: list
    mode: str
    audited_at: str
    value: value.ValueReport | None = None


def _mode(state: dict) -> str:
    return "fixed-horizon" if state.get("stopping_rule") == prereg.STOP_FIXED_N else "sequential"


def analyse_rows(rows: list, mode: str, audited_at: str, vpo: float | None) -> Analysis:
    """Audit, estimate and price `rows`. Pure in (rows, mode, audited_at, vpo), which
    is what lets `verify` reproduce it exactly from the exported CSV."""
    from commontrace.commands import experiment_cmd  # a command module; imported lazily

    at = datetime.datetime.fromisoformat(audited_at)
    report = integrity.audit(rows, now=at)
    effects = experiment.analyze(
        experiment_cmd._observations(rows), sequential=(mode == "sequential"))
    priced = value.compute(
        effects, report, value_per_occasion=vpo,
        overlap=value.overlap_from_assignments(rows), assignments=rows)
    return Analysis(rows, report, effects, mode, audited_at, priced)


def _current_rows(root: str) -> list:
    from commontrace.commands import experiment_cmd

    all_rows, _rate, _corrupt = experiment_cmd._load(root)
    rows, _salt, _other = experiment_cmd.scope_to_current_salt(root, all_rows)
    return rows


# --- Status ---------------------------------------------------------------------------


@dataclass
class Status:
    label: str
    synthetic: bool
    kit_key: str
    started_at: str
    days_elapsed: float
    planned_occasions: int
    occasions: int
    resolved_occasions: int
    progress: float
    projected_days_remaining: float | None
    state: str                      # no-data | collecting | ready | compromised
    integrity: str
    findings: list = field(default_factory=list)
    memories: list = field(default_factory=list)
    prereg_note: str = ""
    deviations: list = field(default_factory=list)
    next_step: str = ""


def status(root: str, *, now: datetime.datetime | None = None) -> Status:
    state = load_state(root)
    if state is None:
        raise ProofError("no proof has been started here (`commontrace proof start`)")
    moment = _now(now)
    rows = _current_rows(root)
    started = datetime.datetime.fromisoformat(state["started_at"])
    elapsed = max(0.0, (moment - started).total_seconds() / 86400)
    occasions = len({r.occasion_id for r in rows})
    resolved = len({r.occasion_id for r in rows if r.succeeded is not None})
    planned = int(state["planned_occasions"])
    registration = prereg.Preregistration.from_dict(state["preregistration"])
    base = dict(
        label=state["label"], synthetic=bool(state.get("synthetic")), kit_key=state["kit"]["key"],
        started_at=state["started_at"], days_elapsed=round(elapsed, 1), planned_occasions=planned,
        occasions=occasions, resolved_occasions=resolved,
        progress=min(1.0, resolved / planned) if planned else 0.0,
    )
    if not rows:
        return Status(**base, projected_days_remaining=None, state="no-data", integrity="",
                      prereg_note=prereg.check(registration).note,
                      next_step="No assignments yet. Retrieve with an occasion id and report the outcome "
                                "under the same id (`commontrace function show` has the occasion and outcome model).")
    analysis = analyse_rows(rows, _mode(state), moment.isoformat(), state.get("value_per_occasion"))
    check = prereg.check(
        registration, actual_salt=rows[0].salt,
        actual_holdout_rate=sum(r.rate for r in rows) / len(rows),
        actual_detectable=state["effect"], actual_occasions=resolved,
        first_observation_at=min((r.at for r in rows if r.at is not None), default=None),
        actual_primary_outcome=registration.primary_outcome)
    memories = [dataclasses.asdict(e) for e in analysis.effects]
    undecided = [e for e in analysis.effects if e.verdict == experiment.VERDICT_UNDERPOWERED]
    rate_per_day = resolved / elapsed if elapsed > 0.01 and resolved else None
    remaining = None
    if rate_per_day and resolved < planned:
        remaining = round((planned - resolved) / rate_per_day, 1)
    if not analysis.report.readable:
        phase, step = "compromised", ("The experiment cannot be trusted as it stands; no effect or "
                                      "value is stated. Fix what the findings name, then start a fresh "
                                      "randomization (`commontrace experiment --configure`).")
    elif undecided and resolved < planned:
        phase, step = "collecting", (f"{len(undecided)} memory/memories not yet decided; "
                                     f"{planned - resolved:,} more resolved occasions planned.")
    else:
        phase, step = "ready", "Enough to report: `commontrace proof report`."
    findings = [
        {"severity": f.severity, "check": f.check, "headline": f.headline}
        for f in analysis.report.findings if f.severity != integrity.SEVERITY_OK
    ]
    return Status(**base, projected_days_remaining=remaining, state=phase,
                  integrity=analysis.report.verdict, findings=findings, memories=memories,
                  prereg_note=check.note, deviations=[d.headline for d in check.deviations],
                  next_step=step)


def render_status(s: Status) -> str:
    banner = "**SYNTHETIC DEMO DATA -- not a customer measurement.**\n\n" if s.synthetic else ""
    lines = [
        f"# Proof: {s.label}", "", banner + f"State: **{s.state}**  ·  {s.resolved_occasions:,} of "
        f"{s.planned_occasions:,} planned occasions resolved ({s.progress:.0%}), day {s.days_elapsed:g}",
    ]
    if s.projected_days_remaining is not None:
        lines.append(f"At the current pace the plan completes in ~{s.projected_days_remaining:g} days.")
    if s.integrity:
        lines += ["", f"Validity: **{s.integrity}**"]
        lines += [f"- [{f['severity']}] {f['headline']}" for f in s.findings]
    if s.memories:
        lines += ["", "| Memory | Verdict | Effect | 95% CI | Injected / withheld |", "|---|---|---|---|---|"]
        for m in s.memories:
            lines.append(
                f"| `{m['lesson_slug']}` | {m['verdict']} | {m['effect']:+.1%} | "
                f"[{m['ci_low']:+.1%}, {m['ci_high']:+.1%}] | {m['n_injected']} / {m['n_withheld']} |")
    lines += ["", s.prereg_note] + [f"- deviation: {d}" for d in s.deviations]
    lines += ["", f"**Next:** {s.next_step}"]
    return "\n".join(lines)


# --- The package ----------------------------------------------------------------------

_NOT_SHOWN = (
    "This report does not show that the agent honoured a memory it was told to withhold "
    "(that leaves no trace and biases the effect toward zero), that outcomes were reported "
    "honestly, or that the registration predates the data beyond what the assignment log's "
    "own timestamps say. A signature authenticates who issued this package, not that the "
    "issuer was honest about what it ran; that is what the raw data and `commontrace proof "
    "verify` are for."
)


def _headline(state: dict, a: Analysis, final: bool) -> str:
    if not a.report.readable:
        return ("**No figure is stated.** The experiment is COMPROMISED by a named mechanism "
                "(below); every effect computed from it would be biased in a direction nobody "
                "can state.")
    helps = [e for e in a.effects if e.verdict == experiment.VERDICT_HELPS]
    hurts = [e for e in a.effects if e.verdict == experiment.VERDICT_HURTS]
    undecided = [e for e in a.effects if e.verdict == experiment.VERDICT_UNDERPOWERED]
    parts = [f"{len(helps)} memory/memories HELP", f"{len(hurts)} HURT",
             f"{len(a.effects) - len(helps) - len(hurts) - len(undecided)} show no measurable effect"]
    if undecided:
        parts.append(f"{len(undecided)} not yet decided")
    text = "; ".join(parts) + "."
    if not final:
        text += " **Interim: the planned sample has not been reached, so this can still change.**"
    return text


def _memory_rows(a: Analysis) -> list[dict]:
    return [dataclasses.asdict(e) for e in a.effects]


def harm_recovery(a: Analysis, vpo: float | None) -> dict | None:
    """What withdrawing the harmful memories would give back over the measured window.

    The measured loss of each memory whose verdict is HURTS, sign-flipped, with its
    interval. A per-memory figure, so it stands even where the memories overlap and
    cannot be summed into a total (value.py rule 4); here it is only ever summed
    over memories that were counted, and only when the run is readable. It looks
    backward at what was measured; it is not a forecast and is never billed.
    None when there is nothing to recover or nothing can be stated.
    """
    if a.value is None or not a.value.readable:
        return None
    lost = [m for m in a.value.memories if m.verdict == experiment.VERDICT_HURTS and m.counted]
    if not lost:
        return None
    occasions = -sum(m.occasions_improved for m in lost)
    # In quadrature, the same convention value.compute uses for its aggregate.
    spread_low = sum((m.occasions_improved - m.ci_low) ** 2 for m in lost) ** 0.5
    spread_high = sum((m.ci_high - m.occasions_improved) ** 2 for m in lost) ** 0.5
    rate = a.value.rate if vpo else None
    return {
        "memories": [m.slug for m in lost], "occasions": occasions,
        "ci_95": [occasions - spread_high, occasions + spread_low],
        "money": occasions * rate if rate else None,
    }


def build_record(root: str, state: dict, *, key: bytes | None, org_id: str,
                 now: datetime.datetime | None = None) -> tuple[dict, str, Analysis]:
    """The machine-readable record, the raw CSV text, and the analysis behind them."""
    moment = _now(now)
    rows = _current_rows(root)
    if not rows:
        raise ProofError("no assignments to report on yet")
    audited_at = moment.isoformat()
    a = analyse_rows(rows, _mode(state), audited_at, state.get("value_per_occasion"))
    export = raw_export.export(rows)
    registration = prereg.Preregistration.from_dict(state["preregistration"])
    check = prereg.check(
        registration, actual_salt=rows[0].salt,
        actual_holdout_rate=sum(r.rate for r in rows) / len(rows),
        actual_detectable=state["effect"],
        actual_occasions=len({r.occasion_id for r in rows if r.succeeded is not None}),
        first_observation_at=min((r.at for r in rows if r.at is not None), default=None),
        actual_primary_outcome=registration.primary_outcome)
    resolved = len({r.occasion_id for r in rows if r.succeeded is not None})
    final = resolved >= int(state["planned_occasions"]) or all(
        e.verdict != experiment.VERDICT_UNDERPOWERED for e in a.effects)
    ledger = a.value.ledger() if a.value is not None else []
    signature = None
    if key is not None:
        signature = value.sign_ledger(
            ledger, key, org_id=org_id, issued_at=audited_at,
            evidence_digest=export.digest, prereg_fingerprint=check.fingerprint)
    policy = retrieval_io.read_harm_policy(root)
    hurts = [e.lesson_slug for e in a.effects if e.verdict == experiment.VERDICT_HURTS]
    record = {
        "schema": SCHEMA, "label": state["label"], "synthetic": bool(state.get("synthetic")),
        "final": final, "mode": a.mode, "audited_at": audited_at, "org_id": org_id,
        "design": {k: state[k] for k in ("kit", "baseline", "effect", "rate", "daily_occasions",
                                         "planned_occasions", "value_per_occasion", "started_at")},
        "preregistration": state["preregistration"], "preregistration_fingerprint": check.fingerprint,
        "preregistration_clean": check.clean, "deviations": [d.headline for d in check.deviations],
        "integrity": {"verdict": a.report.verdict, "readable": a.report.readable,
                      "findings": [dataclasses.asdict(f) for f in a.report.findings]},
        "memories": _memory_rows(a),
        "harmful": {"memories": hurts, "store_policy": policy,
                    "withdrawn_automatically": bool(hurts) and policy == "withdraw",
                    "recoverable": harm_recovery(a, state.get("value_per_occasion"))},
        "value": {
            "readable": a.value.readable, "reason": a.value.reason,
            "aggregate_readable": a.value.aggregate_readable,
            "occasions_improved": a.value.occasions_improved,
            "ci_95": [a.value.ci_low, a.value.ci_high], "money": a.value.money,
            "value_per_occasion": state.get("value_per_occasion"),
            "memories": [dataclasses.asdict(m) for m in a.value.memories],
        },
        "ledger": [dataclasses.asdict(e) for e in ledger],
        "ledger_root": value.ledger_root(ledger),
        "evidence": {"digest": export.digest, "rows": export.n_rows, "occasions": export.n_occasions,
                     "file": DATA_NAME},
        "signature": signature,
        "signed_over": ["org_id", "audited_at", "ledger_root", "evidence.digest",
                        "preregistration_fingerprint"],
    }
    return record, export.csv_text, a


def render_report(record: dict, a: Analysis) -> str:
    d = record["design"]
    kit = d["kit"]
    lines = [f"# Agent Learning Proof: {record['label']}", ""]
    if record["synthetic"]:
        lines += ["> **SYNTHETIC DEMO DATA -- not a customer measurement.** Outcomes were drawn by the "
                  "tool from planted effects so this report can be shown before there is real data.", ""]
    lines += [
        f"Function: **{kit['title']}**. Occasion: one {kit['occasion']['label']}. "
        f"Success: {kit['outcome']['success']}. Window: {kit['outcome']['window_days']:g} days.",
        "", "## Result", "", _headline(record, a, record["final"]), "",
        integrity.render(a.report), "", "## What each memory did", "",
        "| Memory | Verdict | Effect | 95% CI | Injected | Withheld |", "|---|---|---|---|---|---|",
    ]
    for m in record["memories"]:
        lines.append(f"| `{m['lesson_slug']}` | **{m['verdict']}** | {m['effect']:+.1%} | "
                     f"[{m['ci_low']:+.1%}, {m['ci_high']:+.1%}] | {m['n_injected']} | {m['n_withheld']} |")
    lines.append("")
    if a.mode == "sequential":
        lines += ["_Intervals are anytime-valid: they stay valid however often the run was looked at, "
                  "which is why they are wider than a single look at a finished run would give._", ""]
    h = record["harmful"]
    if h["memories"]:
        lines += ["## Harmful memories", "",
                  "These made outcomes WORSE: " + ", ".join(f"`{s}`" for s in h["memories"]) + ". "
                  + ("The store's policy is `withdraw`, so they are no longer delivered."
                     if h["withdrawn_automatically"] else
                     "The store's policy is `inform`, so they are still being delivered: "
                     "`commontrace retrieval --on-harm withdraw` stops that."), ""]
        rec = h.get("recoverable")
        if rec:
            lo, hi = rec["ci_95"]
            money = f" (about {rec['money']:,.2f} at your rate)" if rec["money"] is not None else ""
            lines += [f"Over the measured window they cost about **{rec['occasions']:,.0f} occasions**{money}, "
                      f"95% interval {lo:,.0f} to {hi:,.0f}. That is what stopping them gives back; it "
                      "is measured, not forecast, and it is not billed.", ""]
    if a.value is not None:
        lines += [value.render(a.value), ""]
    lines += ["## Pre-registration", "",
              ("Registered before the run and run as registered." if record["preregistration_clean"]
               else "Registered, with deviations the reader should weigh:"), ""]
    lines += [f"- {x}" for x in record["deviations"]]
    lines += [f"- fingerprint `{record['preregistration_fingerprint']}`", ""]
    lines += ["## Ledger and signature", ""]
    if record["ledger"]:
        lines += ["| # | Memory | Verdict | Occasions improved | Rate | Amount |", "|---|---|---|---|---|---|"]
        for e in record["ledger"]:
            lines.append(f"| {e['index']} | `{e['slug']}` | {e['verdict']} | {e['occasions_improved']:+,.1f} "
                         f"| {e['rate']:g} | {e['money']:,.2f} |")
        lines.append("")
    else:
        lines += ["No ledger: nothing is billable here (no agreed value per occasion, an unreadable "
                  "experiment, or nothing established).", ""]
    lines += [f"Ledger root `{record['ledger_root']}`", f"Data digest `{record['evidence']['digest']}`", ""]
    lines += [("Signed by the issuer over the ledger root, the data digest and the pre-registration "
               f"fingerprint (HMAC-SHA256, org `{record['org_id']}`, {record['audited_at']})."
               if record["signature"] else
               "**Not signed.** The chain and the data digest still check; without an issuer "
               "signature a party with write access could replace the whole package.")]
    lines += ["", "## Check it yourself", "",
              f"`{RECORD_NAME}` is this report in machine-readable form and `{DATA_NAME}` is one row per "
              "arm decision, including those that never got an outcome. Run:", "",
              "    commontrace proof verify <this directory> [--key-file KEY]", "",
              "It recomputes the audit, every estimate and the value arithmetic from the CSV alone, "
              "and checks the digest, the ledger chain, the pre-registration fingerprint and the "
              "signature. The digest rule is in `commontrace/raw_export.py` and is short enough to "
              "reimplement in any language.", "", "## What this does not show", "", _NOT_SHOWN, ""]
    return "\n".join(lines)


def read_key(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
    except OSError as exc:
        raise ProofError(f"cannot read the key file: {exc.strerror or exc}") from None
    if len(key) < MIN_KEY_BYTES:
        raise ProofError(f"a signing key under {MIN_KEY_BYTES} bytes can be guessed; use a longer one "
                         "(e.g. `openssl rand -hex 32`)")
    return key


def build(root: str, out_dir: str, *, key: bytes | None = None, org_id: str = "",
          now: datetime.datetime | None = None) -> dict:
    """Write the package into `out_dir` and return its record."""
    state = load_state(root)
    if state is None:
        raise ProofError("no proof has been started here (`commontrace proof start`)")
    record, csv_text, a = build_record(root, state, key=key, org_id=org_id or state["label"], now=now)
    os.makedirs(out_dir, exist_ok=True)
    from commontrace import proof_page

    for name, text in ((MARKDOWN_NAME, render_report(record, a)),
                       (PAGE_NAME, proof_page.render(record)),
                       (RECORD_NAME, json.dumps(record, indent=2, sort_keys=True) + "\n"),
                       (DATA_NAME, csv_text)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return record


# --- Verify ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str = ""


def rows_from_csv(csv_text: str) -> list[integrity.Assignment]:
    """The assignment rows an export describes. Raises ProofError on a file that
    is not one."""
    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader, None)
    if tuple(header or ()) != raw_export.COLUMNS:
        raise ProofError("assignments.csv does not have the expected columns")

    def when(cell: str):
        return datetime.datetime.fromisoformat(cell) if cell else None

    rows = []
    for number, cells in enumerate(reader, start=2):
        if not cells:
            continue
        try:
            memory, occasion, arm, succeeded, assigned, resolved, revision, rate, salt = cells
            rows.append(integrity.Assignment(
                lesson=memory, occasion_id=occasion, injected=(arm == "injected"), rate=float(rate),
                salt=salt, succeeded={"true": True, "false": False, "": None}[succeeded],
                at=when(assigned), resolved_at=when(resolved), revision=revision or None))
        except (ValueError, KeyError) as exc:
            raise ProofError(f"assignments.csv row {number} is unreadable: {exc}") from None
    return rows


_TOL = 1e-9


def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= _TOL
        except (TypeError, ValueError):
            return False
    return a == b


def _diff_memories(recorded: list[dict], recomputed: list[dict]) -> list[str]:
    out = []
    by_slug = {m["lesson_slug"]: m for m in recomputed}
    for m in recorded:
        other = by_slug.get(m["lesson_slug"])
        if other is None:
            out.append(f"{m['lesson_slug']}: not present in the recomputation")
            continue
        for k in ("verdict", "effect", "ci_low", "ci_high", "n_injected", "n_withheld", "significant"):
            if not _same(m.get(k), other.get(k)):
                out.append(f"{m['lesson_slug']}.{k}: report says {m.get(k)!r}, data gives {other.get(k)!r}")
    for slug in set(by_slug) - {m["lesson_slug"] for m in recorded}:
        out.append(f"{slug}: in the data but missing from the report")
    return out


def verify(directory: str, *, key: bytes | None = None) -> list[Check]:
    """Re-derive the package from its own raw data. Never trusts a number it can recompute."""
    try:
        with open(os.path.join(directory, RECORD_NAME), encoding="utf-8") as fh:
            record = json.load(fh)
        with open(os.path.join(directory, DATA_NAME), encoding="utf-8", newline="") as fh:
            csv_text = fh.read()
    except (OSError, ValueError) as exc:
        raise ProofError(f"cannot read the package in {directory}: {exc}") from None
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise ProofError("proof.json is not a package this version can read")

    checks: list[Check] = []
    digest = record["evidence"]["digest"]
    checks.append(Check("data digest", PASS if raw_export.verify(csv_text, digest) else FAIL,
                        "assignments.csv hashes to the digest the package commits to"
                        if raw_export.verify(csv_text, digest) else
                        "assignments.csv does not hash to the recorded digest: the data was changed"))

    try:
        rows = rows_from_csv(csv_text)
        vpo = record["design"].get("value_per_occasion")
        a = analyse_rows(rows, record["mode"], record["audited_at"], vpo)
    except (ProofError, ValueError) as exc:
        checks.append(Check("recomputation", FAIL, str(exc)))
        return checks

    problems = _diff_memories(record["memories"], _memory_rows(a))
    if record["integrity"]["verdict"] != a.report.verdict:
        problems.append(f"integrity: report says {record['integrity']['verdict']}, data gives {a.report.verdict}")
    checks.append(Check(
        "recomputed estimates", FAIL if problems else PASS,
        "; ".join(problems[:6]) if problems else
        f"{len(a.effects)} memories and the validity audit recomputed from {len(rows)} rows"))

    recovery = harm_recovery(a, vpo)
    recorded_recovery = record["harmful"].get("recoverable")
    same_recovery = (recovery is None) == (recorded_recovery is None) and (
        recovery is None or (
            recovery["memories"] == recorded_recovery["memories"]
            and _same(recovery["occasions"], recorded_recovery["occasions"])
            and all(_same(x, y) for x, y in zip(recovery["ci_95"], recorded_recovery["ci_95"]))
            and (recovery["money"] is None) == (recorded_recovery["money"] is None)
            and (recovery["money"] is None or _same(recovery["money"], recorded_recovery["money"]))))
    checks.append(Check("recomputed harm recovery", PASS if same_recovery else FAIL,
                        "what withdrawing the harmful memories would give back follows from the data"
                        if same_recovery else "the harm-recovery figure does not follow from the data"))

    ledger = a.value.ledger() if a.value is not None else []
    recomputed = [dataclasses.asdict(e) for e in ledger]
    recorded = record["ledger"]
    same = len(recomputed) == len(recorded) and all(
        all(_same(x[k], y[k]) for k in x) for x, y in zip(recorded, recomputed))
    checks.append(Check("recomputed value ledger", PASS if same else FAIL,
                        f"{len(recorded)} line(s) match the arithmetic on the data" if same else
                        "the ledger does not follow from the data and the agreed rate"))

    entries = [value.LedgerEntry(**e) for e in recorded]
    bad = value.verify_ledger(entries)
    checks.append(Check("ledger chain", PASS if bad is None else FAIL,
                        "every line follows from the one before it" if bad is None else
                        f"line {bad} does not follow from the previous: the ledger was edited"))

    try:
        registration = prereg.Preregistration.from_dict(record["preregistration"])
        fingerprint_ok = registration.fingerprint() == record["preregistration_fingerprint"]
    except prereg.PreregError as exc:
        fingerprint_ok, _ = False, exc
    checks.append(Check("pre-registration", PASS if fingerprint_ok else FAIL,
                        "the registered design matches its fingerprint" if fingerprint_ok else
                        "the registered design does not match its fingerprint: it was changed"))

    if not record.get("signature"):
        checks.append(Check("issuer signature", UNSIGNED,
                            "this package carries no signature, so who issued it is not established"))
    elif key is None:
        checks.append(Check("issuer signature", SKIP,
                            "a signature is present; pass the issuer's key (--key-file) to check it"))
    else:
        ok = value.verify_ledger_signature(
            entries, record["signature"], key, org_id=record["org_id"], issued_at=record["audited_at"],
            evidence_digest=digest, prereg_fingerprint=record["preregistration_fingerprint"])
        checks.append(Check("issuer signature", PASS if ok else FAIL,
                            "signed by the holder of this key over this ledger, data and design" if ok else
                            "the signature does not verify: wrong key, or the package was changed"))
    return checks


def verified(checks: list[Check]) -> bool:
    return all(c.status != FAIL for c in checks)


def render_checks(checks: list[Check]) -> str:
    return "\n".join(f"[{c.status:8s}] {c.name}: {c.detail}" for c in checks)
