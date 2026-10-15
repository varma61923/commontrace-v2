from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CANON = "Capture \u2192 Structure \u2192 Extract \u2192 Validate \u2192 Store \u2192 Inject \u2192 Measure"
ASCII_CHAIN = "Capture -> Structure -> Extract"


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_no_ascii_arrow_fork():
    checked = []
    for dirpath, _dirnames, filenames in os.walk(ROOT):
        norm_dirpath = dirpath.replace(os.sep, "/")
        if ".git" in norm_dirpath or "/tests" in norm_dirpath:
            continue
        for fn in filenames:
            if fn.endswith((".md", ".py")):
                full = os.path.join(dirpath, fn)
                checked.append(full)
                with open(full, encoding="utf-8", errors="replace") as fh:
                    assert ASCII_CHAIN not in fh.read(), (
                        f"{full} states pipeline names with ASCII arrows"
                    )
    assert checked
