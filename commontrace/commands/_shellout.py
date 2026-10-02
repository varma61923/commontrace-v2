from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from typing import Literal, overload

from commontrace import warm


def has_attention_deps() -> bool:
    return (
        importlib.util.find_spec("numpy") is not None
        and importlib.util.find_spec("sentence_transformers") is not None
    )


def packaged_reference_dir() -> str:
    """Where reference scripts that ship inside the wheel live."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reference")


def _store_scripts_allowed() -> bool:
    return os.environ.get("COMMONTRACE_ALLOW_STORE_SCRIPTS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def find_reference_script(root: str, relative: str) -> str | None:
    """Locate a reference-implementation script (attention/query.py, benchmark/...)."""
    if _store_scripts_allowed():
        store_copy = os.path.abspath(os.path.join(root, relative))
        root_abs = os.path.abspath(root)
        if not (store_copy == root_abs or store_copy.startswith(root_abs + os.sep)):
            return None
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


@overload
def run_script(
    root: str, relative: str, extra_args: list[str], missing_hint: str,
    capture: Literal[False] = False,
    extra_env: dict[str, str] | None = None,
) -> int: ...


@overload
def run_script(
    root: str, relative: str, extra_args: list[str], missing_hint: str,
    capture: Literal[True],
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str]: ...


def run_script(
    root: str,
    relative: str,
    extra_args: list[str],
    missing_hint: str,
    capture: bool = False,
    extra_env: dict[str, str] | None = None,
) -> int | tuple[int, str]:
    """Run a reference script as a subprocess."""
    script = find_reference_script(root, relative)
    if script is None:
        print(
            f"[commontrace] Could not find {relative}. {missing_hint}",
            file=sys.stderr,
        )
        return (1, "") if capture else 1
    env = dict(os.environ)
    env["COMMONTRACE_ROOT"] = root
    env["PYTHONUTF8"] = "1"
    env["PYTHONSAFEPATH"] = "1"
    if extra_env:
        env.update(extra_env)

    if not extra_env and warm.enabled(relative):
        answered = warm.run(
            warm.worker_script(relative, script), extra_args, root, env,
            build=warm.worker_script(relative, script) != script,
        )
        if answered is not None:
            rc, out, err = answered
            if err:
                sys.stderr.write(err)
                sys.stderr.flush()
            if capture:
                return rc, out
            sys.stdout.write(out)
            sys.stdout.flush()
            return rc

    cmd = [sys.executable]
    if sys.version_info >= (3, 11):
        cmd.append("-P")
    cmd.extend([script, *extra_args])

    if capture:
        result = subprocess.run(
            cmd, env=env,
            stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
        return result.returncode, result.stdout
    result = subprocess.run(cmd, env=env)
    return result.returncode
