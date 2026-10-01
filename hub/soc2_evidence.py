"""SOC 2 evidence that is checked, not asserted: `SOC2_READINESS.md` against the repository at a given commit.

The mapping document says, for each control, where the evidence lives (`hub/scopes.py`,
`hub/tests/test_api_key_scopes.py (22 tests)`, `hub/db.py:check_row_level_security`). A document like that rots
silently: a file is renamed, a test is deleted, a count drifts. This resolves every reference and reports what no
longer exists, and with `--run-tests` runs the tests it cites, so a point-in-time bundle (commit, date, results)
can be handed to an auditor.

    python -m hub.soc2_evidence                      # resolve every reference; exit 1 if any is dangling
    python -m hub.soc2_evidence --run-tests --out evidence/

What this does not do: it does not decide that a control is adequate, and it does not replace a Type II engagement,
which needs months of operating evidence (access reviews, change tickets, incident records) that no repository
holds. It makes the part that IS in the repository current and checkable.

References understood, all inside backticks:  `path`, `path:symbol`, `path::test_name`, with an optional
"(N tests)" after a test file. Paths are resolved from the repository root; a claimed count is checked against what
pytest runs (`--run-tests`). A reference that is not a path (`HUB_ENCRYPTION_KEY`, `/healthz`) is not a claim
about a file and is ignored.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = "SOC2_READINESS.md"
_CODE = re.compile(r"`([^`]+)`")
_PATH = re.compile(r"^(?:[\w.\-]+/)*[\w.\-]+\.(?:py|md|yml|yaml|json|toml|txt|sh|sql)$")


@dataclass
class Ref:
    control: str
    raw: str
    path: str
    symbol: str = ""
    claimed_tests: int | None = None
    ok: bool = True
    problem: str = ""


@dataclass
class Report:
    commit: str = ""
    at: str = ""
    refs: list = field(default_factory=list)
    tests: list = field(default_factory=list)

    @property
    def dangling(self) -> list:
        return [r for r in self.refs if not r.ok]


def parse(text: str) -> list[Ref]:
    """Every file reference in the Evidence column of the document's tables."""
    refs: list[Ref] = []
    for line in text.splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() == "criterion":
            continue
        control = cells[0]
        evidence = cells[-1]
        count = re.search(r"\((\d+) tests?\)", evidence)
        for raw in _CODE.findall(evidence):
            token = raw.strip()
            symbol = ""
            if "::" in token:
                token, symbol = token.split("::", 1)
            elif ":" in token and not token.startswith("http"):
                token, symbol = token.split(":", 1)
            if not _PATH.match(token):
                continue
            refs.append(Ref(control=control, raw=raw, path=token, symbol=symbol.strip(),
                            claimed_tests=int(count.group(1)) if count and token.startswith("hub/tests/") else None))
    return refs


def resolve(ref: Ref, root: str = ROOT) -> Ref:
    full = os.path.join(root, ref.path)
    if not os.path.isfile(full):
        ref.ok, ref.problem = False, "file does not exist"
        return ref
    try:
        text = open(full, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        ref.ok, ref.problem = False, f"unreadable: {exc}"
        return ref
    if ref.symbol:
        # `Class.method`, `TestClass::test_x`, `module:function`: every dotted part must appear in the file.
        for part in re.split(r"[./]", ref.symbol):
            part = part.strip("() ")
            if part and part not in text:
                ref.ok, ref.problem = False, f"{part!r} not found in {ref.path}"
                return ref
    return ref


def build(root: str = ROOT) -> Report:
    with open(os.path.join(root, DOC), encoding="utf-8") as fh:
        refs = [resolve(r, root) for r in parse(fh.read())]
    commit = ""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                                timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return Report(commit=commit, at=datetime.datetime.now(datetime.timezone.utc).isoformat(), refs=refs)


def run_tests(report: Report, root: str = ROOT) -> None:
    """Run each distinct cited test file once and record the outcome. A claimed count ("(22 tests)") is checked
    here, against what pytest actually collected and ran, because parametrized tests make a count of definitions
    meaningless."""
    claims = {r.path: r.claimed_tests for r in report.refs if r.claimed_tests is not None}
    files = sorted({r.path for r in report.refs if r.path.startswith(("hub/tests/", "tests/")) and r.ok})
    for path in files:
        proc = subprocess.run([sys.executable, "-m", "pytest", path, "-q", "-p", "no:cacheprovider"], cwd=root,
                              capture_output=True, text=True)
        tail = (proc.stdout.strip().splitlines() or [""])[-1]
        ran = re.search(r"(\d+) passed", tail)
        short = path in claims and (not ran or int(ran.group(1)) < claims[path])
        report.tests.append({"file": path, "passed": proc.returncode == 0 and not short, "summary": tail
                             + (f" (the document claims {claims[path]})" if short else "")})


def render(report: Report) -> str:
    lines = ["# SOC 2 evidence bundle", "", f"Commit `{report.commit or 'unknown'}`, {report.at}.", "",
             f"{len(report.refs)} references in `{DOC}`; {len(report.dangling)} dangling.", ""]
    for r in report.dangling:
        lines.append(f"- DANGLING `{r.raw}` ({r.control}): {r.problem}")
    if report.tests:
        lines += ["", "## Cited tests", ""]
        lines += [f"- {'PASS' if t['passed'] else 'FAIL'} `{t['file']}`: {t['summary']}" for t in report.tests]
    lines += ["", "This is evidence that what the document points at exists and (if run) passes at this commit. "
              "It is not an assessment of control adequacy, and it is not a Type II report."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-tests", action="store_true", help="also run every test file the document cites")
    ap.add_argument("--out", default=None, help="write evidence.md and evidence.json here")
    args = ap.parse_args(argv)
    report = build()
    if args.run_tests:
        run_tests(report)
    text = render(report)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "evidence.md"), "w", encoding="utf-8") as fh:
            fh.write(text)
        with open(os.path.join(args.out, "evidence.json"), "w", encoding="utf-8") as fh:
            json.dump({"commit": report.commit, "at": report.at, "refs": [asdict(r) for r in report.refs],
                       "tests": report.tests}, fh, indent=2)
    print(text, end="")
    return 1 if report.dangling or any(not t["passed"] for t in report.tests) else 0


if __name__ == "__main__":
    sys.exit(main())
