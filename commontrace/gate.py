"""Can this store's memory be shipped? A yes/no a CI job can enforce."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

from commontrace import experiment, frontmatter, harm, injection_guard, memory_guard, paths, reliability, templates

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str
    subject: str = ""


@dataclass
class GateResult:
    checks: list[Check] = field(default_factory=list)
    strict: bool = False

    @property
    def failed(self) -> list[Check]:
        bad = {FAIL, WARN} if self.strict else {FAIL}
        return [c for c in self.checks if c.status in bad]

    @property
    def passed(self) -> bool:
        return not self.failed


def _active_lessons(root: str) -> list[tuple[str, dict, str]]:
    ldir = paths.lessons_dir(root)
    out: list[tuple[str, dict, str]] = []
    if not os.path.isdir(ldir):
        return out
    for name in sorted(os.listdir(ldir)):
        if not (name.startswith("lesson_") and name.endswith(".md")) or name == "lesson_template.md":
            continue
        try:
            fm, body = frontmatter.read(os.path.join(ldir, name))
        except Exception:  # noqa: BLE001 - an unreadable lesson is reported by doctor, not here
            continue
        if str(fm.get("status")) == "active":
            out.append((str(fm.get("name") or name[len("lesson_"):-3]), fm, body))
    return out


def _guard_fields(fm: dict, body: str) -> dict:
    return {
        "description": fm.get("description", ""),
        "applies_when": fm.get("applies_when", ""),
        "do_not_apply_when": fm.get("do_not_apply_when", ""),
        "importance_rationale": fm.get("importance_rationale", ""),
        "body": body,
    }


def run(root: str, *, strict: bool = False) -> GateResult:
    """Every check, against the store at `root`."""
    from commontrace import evidence, retrieval_io

    result = GateResult(strict=strict)
    lessons = _active_lessons(root)
    by_slug = {slug: fm for slug, fm, _body in lessons}

    analysis = evidence.analyse(root)
    report = analysis.report
    if report is None:
        result.checks.append(Check("experiment", PASS, "no experiment data yet"))
    elif not report.readable:
        reasons = "; ".join(f.headline for f in report.blocking) or report.verdict
        result.checks.append(Check("experiment", FAIL, f"experiment is COMPROMISED: {reasons}"))
    else:
        result.checks.append(Check("experiment", PASS, f"experiment is {report.verdict}"))

    policy = retrieval_io.load_config(root).harm_policy
    readable = report is not None and report.readable
    verdicts = {e.lesson_slug: e for e in analysis.effects} if readable else {}
    hurting = [
        e for slug, e in verdicts.items()
        if e.verdict == experiment.VERDICT_HURTS and slug in by_slug and not by_slug[slug].get("core")
    ]
    for e in hurting:
        withdrawn = policy == harm.POLICY_WITHDRAW
        result.checks.append(Check(
            "harm", WARN if withdrawn else FAIL,
            (f"active lesson measured to make outcomes worse (effect {e.effect:+.1%}, "
             f"n={e.n_injected}+{e.n_withheld}); "
             + ("withdrawn from injection by the store's harm policy -- rewrite or archive it"
                if withdrawn else "it is still injected: archive it, or set --on-harm withdraw")),
            subject=e.lesson_slug,
        ))
    if not hurting:
        result.checks.append(Check("harm", PASS, "no active lesson is measured to hurt"))

    if lessons and readable and not any(
        e.verdict in (experiment.VERDICT_HELPS, experiment.VERDICT_HURTS, experiment.VERDICT_NO_EFFECT)
        for e in analysis.effects
    ):
        result.checks.append(Check(
            "underpowered", WARN,
            "no lesson has a verdict yet, so this gate cannot see harm it lacks the data for",
        ))

    unsafe = scaffolded = 0
    for slug, fm, body in lessons:
        fields = _guard_fields(fm, body)
        guard = memory_guard.scan_fields(fields)
        labels = injection_guard.injection_labels(fields)
        if guard.should_block or labels:
            unsafe += 1
            kinds = sorted({f.label for f in guard.blocking_findings} | set(labels))
            result.checks.append(Check(
                "safety", FAIL, "active lesson trips the content screen: " + ", ".join(kinds), subject=slug,
            ))
        if templates.unfilled_placeholders(fm, body):
            scaffolded += 1
            result.checks.append(Check(
                "scaffolding", FAIL, "active lesson still carries unedited TODO scaffolding", subject=slug,
            ))
    if not unsafe:
        result.checks.append(Check("safety", PASS, f"{len(lessons)} active lesson(s) pass the content screen"))
    if not scaffolded:
        result.checks.append(Check("scaffolding", PASS, "no active lesson carries unedited scaffolding"))

    contradictions = reliability.find_contradictions([fm for _s, fm, _b in lessons])
    for c in contradictions:
        result.checks.append(Check(
            "contradiction", WARN,
            f"fires alongside {c.slug_b} (activation overlap {c.activation_overlap:.0%}): " + "; ".join(c.signals),
            subject=c.slug_a,
        ))
    if not contradictions:
        result.checks.append(Check("contradiction", PASS, "no contradicting active lessons found"))
    return result


def render_text(result: GateResult) -> str:
    mark = {PASS: "ok  ", WARN: "warn", FAIL: "FAIL"}
    lines = []
    for c in result.checks:
        subject = f" [{c.subject}]" if c.subject else ""
        lines.append(f"{mark[c.status]}  {c.name}{subject}: {c.message}")
    verdict = "PASSED" if result.passed else f"FAILED ({len(result.failed)} blocking)"
    lines.append(f"\ngate {verdict}" + (" (strict: warnings block)" if result.strict else ""))
    return "\n".join(lines)


def render_json(result: GateResult) -> str:
    return json.dumps({
        "passed": result.passed,
        "strict": result.strict,
        "checks": [c.__dict__ for c in result.checks],
    }, indent=2)


def render_junit(result: GateResult) -> str:
    failures = sum(1 for c in result.checks if c in result.failed)
    rows = []
    for c in result.checks:
        name = quoteattr(f"{c.name}[{c.subject}]" if c.subject else c.name)
        if c in result.failed:
            rows.append(f'    <testcase classname="commontrace.gate" name={name}>'
                        f'<failure message={quoteattr(c.message)}>{escape(c.message)}</failure></testcase>')
        elif c.status == WARN:
            rows.append(f'    <testcase classname="commontrace.gate" name={name}>'
                        f'<system-out>{escape("warning: " + c.message)}</system-out></testcase>')
        else:
            rows.append(f'    <testcase classname="commontrace.gate" name={name}/>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="commontrace gate" tests="{len(result.checks)}" failures="{failures}">\n'
        + "\n".join(rows) + "\n</testsuite>\n"
    )


def render_github(result: GateResult) -> str:
    def esc(s: str) -> str:
        return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    lines = []
    for c in result.checks:
        if c.status == PASS:
            continue
        level = "error" if c in result.failed else "warning"
        title = f"commontrace gate: {c.name}" + (f" ({c.subject})" if c.subject else "")
        lines.append(f"::{level} title={esc(title)}::{esc(c.message)}")
    lines.append(render_text(result))
    return "\n".join(lines)


RENDERERS = {"text": render_text, "json": render_json, "junit": render_junit, "github": render_github}
