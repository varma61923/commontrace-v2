from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from typing import Literal, overload


def has_attention_deps() -> bool:
    """Whether the optional semantic attention layer's deps (numpy, sentence-transformers)
    are importable. Check this before shelling out to memory/attention/*.py -- those scripts
    `import numpy` at module scope, so without this check a missing dep surfaces as a raw
    traceback (internal file paths and all) instead of the `pip install commontrace[attention]`
    hint `doctor` already gives.
    """
    return (
        importlib.util.find_spec("numpy") is not None
        and importlib.util.find_spec("sentence_transformers") is not None
    )


def packaged_reference_dir() -> str:
    """Where reference scripts that ship inside the wheel live.

    Everything here is packaged (see pyproject.toml's package-data), so a
    plain `pip install commontrace` can run `commontrace bench`, `commontrace
    index` and semantic `commontrace query` without a repo checkout.

    The attention scripts used to be excluded on the grounds that they "need
    numpy + sentence-transformers". That conflated two different things: the
    script FILES are plain text and cost nothing to ship, while their
    DEPENDENCIES are a runtime question `has_attention_deps()` already
    answers before anything is invoked. Excluding the files meant a user who
    followed the README's own `pip install commontrace[attention]` got the
    extra installed and semantic retrieval still unreachable -- `query` chose
    the semantic path, failed to find the script, and exited non-zero.
    """
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reference")


def _store_scripts_allowed() -> bool:
    """Whether a reference script may be executed from the store root.

    The store root is attacker-influenced (`--dest` > `$COMMONTRACE_ROOT` >
    cwd): executing `<root>/memory/attention/query.py` by default means a
    cloned/forked store can plant code that the victim runs with their own
    permissions. The packaged copy is preferred; the store-root copy runs
    only with explicit opt-in (`COMMONTRACE_ALLOW_STORE_SCRIPTS=1`), for a
    contributor iterating on the reference scripts in their own checkout.
    """
    return os.environ.get("COMMONTRACE_ALLOW_STORE_SCRIPTS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def find_reference_script(root: str, relative: str) -> str | None:
    """Locate a reference-implementation script (attention/query.py, benchmark/...).

    Checked in order: with `COMMONTRACE_ALLOW_STORE_SCRIPTS=1`, the store
    root first (that opt-in exists so a contributor's edited copy in their
    own checkout is actually picked up -- checking it second, behind the
    always-present packaged copy, made the opt-in permanently unreachable);
    otherwise only the packaged copy is ever considered. The packaged copy
    is what makes the command work for someone who only ran `pip install
    commontrace`, and is the only candidate by default so an untrusted clone
    cannot plant an executable script the victim runs by pointing `--dest`
    (or cwd) at it.
    """
    if _store_scripts_allowed():
        store_copy = os.path.join(root, relative)
        if os.path.isfile(store_copy):
            print(
                "[commontrace] warning: running reference script from store root "
                f"{store_copy} (COMMONTRACE_ALLOW_STORE_SCRIPTS=1); packaged copy "
                "ignored. Only set this for a repo checkout you trust.",
                file=sys.stderr,
            )
            return store_copy
    packaged_copy = os.path.join(packaged_reference_dir(), os.path.basename(relative))
    if os.path.isfile(packaged_copy):
        return packaged_copy
    return None


# `-> int | tuple[int, str]` is honest about the runtime behaviour and useless
# to every caller: a type checker cannot know which arm a given call returns,
# so each of the six call sites that never pass `capture` was flagged
# ("Incompatible return value type", "int object is not iterable") for code
# that is correct. Six false positives in a report is how real findings get
# skimmed past. The overloads say what the flag actually determines.
@overload
def run_script(
    root: str, relative: str, extra_args: list[str], missing_hint: str,
    capture: Literal[False] = False,
) -> int: ...


@overload
def run_script(
    root: str, relative: str, extra_args: list[str], missing_hint: str,
    capture: Literal[True],
) -> tuple[int, str]: ...


def run_script(
    root: str,
    relative: str,
    extra_args: list[str],
    missing_hint: str,
    capture: bool = False,
) -> int | tuple[int, str]:
    """Run a reference script as a subprocess.

    With `capture=True` returns (returncode, stdout) instead of streaming
    stdout straight through, so a caller can post-process the result --
    `query --experiment` needs the emitted lesson list in order to apply the
    randomized holdout to it. stderr is never captured: warnings and errors
    should reach the user immediately either way.
    """
    script = find_reference_script(root, relative)
    if script is None:
        print(
            f"[commontrace] Could not find {relative}. {missing_hint}",
            file=sys.stderr,
        )
        return (1, "") if capture else 1
    env = dict(os.environ)
    # Assign, never setdefault. `root` is already the resolved winner of
    # paths.resolve_root() -- --dest beats $COMMONTRACE_ROOT beats cwd. With
    # setdefault, an exported COMMONTRACE_ROOT survives into the child and
    # silently overrides an explicit --dest, inverting that documented
    # priority. The failure is invisible: `bench --pilot --dest B` renders a
    # normal-looking report full of store A's numbers.
    env["COMMONTRACE_ROOT"] = root
    # PYTHONUTF8 forces the child's interpreter into UTF-8 mode (PEP 540)
    # whether capture is True or False, ensuring consistent encoding across platforms.
    env["PYTHONUTF8"] = "1"
    if capture:
        # Both ends of the pipe pinned to UTF-8 explicitly, not left to the
        # host locale: text=True alone decodes using
        # locale.getpreferredencoding(False), which on Windows is commonly
        # a legacy codepage (cp1252, cp932, ...), and the CHILD's own
        # stdout defaults to the same locale-dependent encoding when
        # (as here) it's redirected to a pipe rather than a real console
        # (PEP 528's UTF-8 console fix does not apply to redirected
        # stdout). Every reference script here writes UTF-8 in practice
        # (this project's file I/O is UTF-8 throughout -- lesson/trace
        # content routinely contains non-ASCII text), so leaving either
        # end to the locale risks a mismatch that mangles that output into
        # mojibake or raises UnicodeDecodeError, depending on the exact
        # bytes involved. errors="replace" so an unexpected
        # undecodable byte still degrades to U+FFFD instead of crashing
        # the parent CLI over the child's stdout.
        result = subprocess.run(
            [sys.executable, script, *extra_args], env=env,
            stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
        return result.returncode, result.stdout
    result = subprocess.run([sys.executable, script, *extra_args], env=env)
    return result.returncode
