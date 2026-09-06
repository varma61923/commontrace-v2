from __future__ import annotations

import argparse
import glob
import importlib.util
import os
import re
import shutil
import sys

from commontrace import frontmatter, paths
from commontrace.commands._shellout import find_reference_script


def _installed(module: str) -> bool:
    """Is `module` importable, decided without importing it.

    Guarded, because `doctor` is the command someone runs when the
    environment is already broken -- which is exactly when a probe is most
    likely to misbehave. `importlib.util.find_spec` walks sys.meta_path, so
    any third-party import hook installed in that interpreter gets to raise
    here, and an unhandled exception from one optional-dependency probe takes
    down the whole report before the checks that would have named the real
    problem. An unanswerable probe is reported as "not installed", which is
    the conservative answer: every caller's absent branch is INFO plus an
    install hint, never a failure.
    """
    if module in sys.modules:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:  # noqa: BLE001 - see above; a probe must not be fatal
        return False


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("doctor", help="Check the environment and store health.")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


# Accumulates failed checks so run() can exit non-zero. Module-level rather
# than threaded through every call site because _check is used a dozen times
# and the alternative is a parameter on each; run() resets it on entry so
# repeated in-process invocations (tests) do not inherit stale state.
_FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "", critical: bool = False) -> None:
    """`critical=True` means the tool cannot function, and only those affect
    the exit code.

    The distinction is load-bearing. A freshly `init`-ed store legitimately
    has zero lessons and a pip-installed client legitimately has no
    benchmark script -- both print [WARN] because they are worth seeing, and
    neither is a failure. Exiting non-zero for them would make day one of
    every install look broken to CI, which is how a health check gets
    ignored and then stops being read at all.
    """
    mark = "OK  " if ok else "WARN"
    line = f"[{mark}] {label}"
    if detail:
        line += f" - {detail}"
    print(line)
    if not ok and critical:
        _FAILURES.append(label)


def _info(label: str, detail: str = "") -> None:
    """For conditions that are expected/normal in a standard client install and need no
    action -- as opposed to _check(..., ok=False), which means something is actually wrong
    and worth fixing. Keeping these off [WARN] means a clean `pip install commontrace` +
    `commontrace init` install doesn't read as having problems it doesn't have."""
    line = "[INFO] " + label
    if detail:
        line += f" - {detail}"
    print(line)


def _legacy_suffix(trace_id: str) -> str:
    """The filename fragment capture_cmd used to compute, before it was made
    injective -- the first 16 characters of the sanitized id."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", trace_id).strip("-.")
    return safe[:16]


def _collision_suspects(root: str) -> int:
    """How many groups of occasions could have overwritten each other.

    Detection leans on the holdout log rather than on the traces, because the
    traces are the thing that was destroyed: the log is append-only JSONL and
    still names every occasion that was ever assigned an arm, including ones
    whose trace file was later clobbered by a sibling.

    A group counts as a suspect when two or more DISTINCT occasion ids share
    a legacy 16-character filename fragment (so they would have collided) and
    at least one of them has no trace on disk while another does. Either half
    alone is innocent -- ids can share a prefix without either being captured
    yet, and an occasion can legitimately have no outcome recorded -- which is
    why both are required before saying anything.
    """
    from commontrace import holdout_io

    try:
        records, _ = holdout_io.read_log(root)
    except Exception:  # noqa: BLE001 - doctor must not fail on a damaged log
        return 0
    if not records:
        return 0

    on_disk: set[str] = set()
    for path in glob.glob(os.path.join(paths.traces_dir(root), "*.md")):
        if os.path.basename(path) == "README.md":
            continue
        try:
            fm, _ = frontmatter.read(path)
        except Exception:  # noqa: BLE001
            continue
        on_disk.add(str(fm.get("id", "")))

    groups: dict[str, set[str]] = {}
    for rec in records:
        groups.setdefault(_legacy_suffix(rec.occasion_id), set()).add(rec.occasion_id)

    suspects = 0
    for ids in groups.values():
        if len(ids) < 2:
            continue
        present = {i for i in ids if i in on_disk}
        if present and len(present) < len(ids):
            suspects += 1
    return suspects


def _declared_agent_type(root: str) -> str | None:
    """What memory/INDEX.md's first line literally says, unvalidated.

    paths.store_agent_type returns what commands will USE, substituting a
    default for anything unusable. Comparing the two is the only way to see
    a store whose declared type is being silently ignored.
    """
    try:
        with open(paths.index_path(root), encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return None
    _, sep, value = first.partition("agent_type:")
    if not sep:
        return None
    return value.strip() or None


def run(args: argparse.Namespace) -> int:
    _FAILURES.clear()
    root = paths.resolve_root(args.dest)
    print(f"[commontrace] doctor - store root: {root}\n")

    _check("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0], critical=True)
    _check("PyYAML importable", _installed("yaml"), critical=True)
    _check("git on PATH", shutil.which("git") is not None)

    has_mem = os.path.isdir(paths.memory_dir(root))
    _check("memory/ store present", has_mem,
           paths.memory_dir(root) if has_mem else "run `commontrace init`", critical=True)

    if has_mem:
        n_lessons = 0
        ldir = paths.lessons_dir(root)
        if os.path.isdir(ldir):
            try:
                n_lessons = len(
                    [
                        f
                        for f in os.listdir(ldir)
                        if f.startswith("lesson_") and f != "lesson_template.md"
                    ]
                )
            except OSError:
                n_lessons = 0
        _check("lessons in store", n_lessons > 0, f"{n_lessons} found")

        # What this store says it is, versus what every command will actually
        # read back. These can disagree silently, and when they do, every
        # trace captured without an explicit --agent-type is stamped with the
        # wrong fleet and `--agent-type <yours>` then matches nothing.
        # Traces this store may already have lost. Nothing is rewritten --
        # the data is gone and only a person can decide what to do about it --
        # but a store that silently dropped captures should not have to
        # discover that from a headcount months later.
        collided = _collision_suspects(root)
        if collided:
            _check(
                "trace filename collisions", False,
                f"{collided} trace file(s) hold fewer captures than were made under "
                "them. Before this was fixed, two occasion ids sharing their first 16 "
                "characters (e.g. TICKET-PROJECT-4711 and -4712) wrote to one filename "
                "and the second silently replaced the first. New captures are safe; "
                "these are already-lost traces. Re-capture them if the source data "
                "still exists.",
            )
        else:
            _check("trace filename collisions", True, "none detected")

        declared = _declared_agent_type(root)
        effective = paths.store_agent_type(root)
        if declared is None:
            _info(
                "store agent_type",
                f"not declared in memory/INDEX.md; commands will assume '{effective}'. "
                "Add `agent_type: <your fleet>` to the first line to make it explicit.",
            )
        elif declared == effective:
            _check("store agent_type", True, f"{declared!r} (any field is valid; taxonomy is open)")
        else:
            _check(
                "store agent_type", False,
                f"memory/INDEX.md declares {declared!r} but commands read back "
                f"{effective!r} -- it is not a valid slug "
                f"({paths.AGENT_TYPE_RE.pattern}). Traces are being stamped "
                f"{effective!r}. Fix the first line of memory/INDEX.md.",
            )

    attention_extra = _installed("numpy") and _installed("sentence_transformers")
    # Labels below are stated NEUTRALLY, not affirmatively.
    #
    # These three checks have an INFO branch for "absent, and that is fine",
    # and each reused the affirmative label from its OK branch -- so a
    # missing extra printed:
    #
    #   [INFO] attention extra installed (numpy + sentence-transformers)
    #          - optional; install with `pip install commontrace[attention]`
    #
    # which asserts the extra IS installed and then tells you to install it.
    # `doctor` is the command an operator runs precisely when something is
    # wrong; a label that contradicts its own detail is the last place to
    # spend someone's attention.
    if attention_extra:
        _check("attention extra (numpy + sentence-transformers)", True, "installed")
    else:
        _info(
            "attention extra (numpy + sentence-transformers)",
            "not installed; optional -- `pip install commontrace[attention]` for semantic retrieval",
        )

    # `commontrace serve` is how an agent with no shell reaches this store, and
    # it fails in the least legible place there is: an MCP client spawns it as
    # a subprocess and reports only that the server exited. Checked here, where
    # someone is already looking for what is wrong.
    if _installed("mcp"):
        _check("MCP SDK (agent-native access via `commontrace serve`)", True, "installed")
    else:
        _info(
            "MCP SDK (agent-native access via `commontrace serve`)",
            "not installed; optional -- `pip install commontrace[serve]` to let agents "
            "that cannot run a shell use this store",
        )

    query_script = find_reference_script(root, "memory/attention/query.py")
    if query_script is not None:
        _check("reference attention/query.py", True, query_script)
    else:
        # It ships inside the package now, so absence means a damaged install
        # rather than "you are not in a repo checkout". This used to be an
        # [INFO] saying absence was expected -- which meant the one command
        # that exists to diagnose a broken retriever reported the breakage as
        # normal, while `commontrace query` exited non-zero for anyone who
        # had installed the attention extra.
        _check("reference attention/query.py", False,
               "missing from the installed package - try `pip install --force-reinstall commontrace`")

    bench_script = find_reference_script(root, "benchmark/measure_performance.py")
    if bench_script is not None:
        _check("benchmark script found", True, bench_script)
    else:
        # It ships inside the package now, so absence means a damaged install
        # rather than "you are not in a repo checkout".
        _check("benchmark script found", False,
               "missing from the installed package - try `pip install --force-reinstall commontrace`")

    # Same reasoning and same install as measure_performance.py above --
    # `commontrace bench --pilot`/`commontrace pilot` import it directly and
    # previously failed with a raw ImportError at runtime even when every
    # check above reported clean, since nothing checked for it specifically.
    pilot_script = find_reference_script(root, "benchmark/pilot_metrics.py")
    if pilot_script is not None:
        _check("pilot metrics script found", True, pilot_script)
    else:
        _check("pilot metrics script found", False,
               "missing from the installed package - try `pip install --force-reinstall commontrace`")

    protocol_dir = os.path.join(root, "protocol")
    if os.path.isdir(protocol_dir):
        _check("protocol/ spec", True, protocol_dir)
    else:
        # Neutral label, same reason as the two above: "protocol/ spec
        # present - not in a repo checkout" claimed the opposite of what it
        # was reporting.
        _info(
            "protocol/ spec",
            "not present; expected for a pip-installed client -- schemas are mirrored "
            "at commontrace/schemas/",
        )

    if _FAILURES:
        # Non-zero so a CI gate, a container health check, or an onboarding
        # script can act on this. Returning 0 unconditionally meant `doctor`
        # could report a missing store, no lessons and an unsupported Python
        # and still look like a pass to everything except a human reading
        # the output. _info conditions are deliberately excluded -- they are
        # normal for a clean client install and must not fail a pipeline.
        print(f"\nDone. {len(_FAILURES)} critical check(s) failed: "
              + ", ".join(_FAILURES))
        return 1

    print("\nDone.")
    return 0
