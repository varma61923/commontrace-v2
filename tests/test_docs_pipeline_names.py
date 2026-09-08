"""Docs-drift guard for the seven protocol stage names (I05).

protocol/PROTOCOL.md's stage table is normative: every restatement across
the repo must reproduce the seven names verbatim (unicode arrows), in
order. An ASCII-arrow restatement (install_cmd.py once had one) silently
forks the vocabulary the fleet uses to describe its own pipeline.
"""
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


def test_canonical_pipeline_names_in_sync():
    for rel in ("protocol/PROTOCOL.md", "README.md", "AGENTS.md"):
        assert CANON in _normalized(_read(rel)), f"{rel} must state the pipeline names verbatim"


def test_no_ascii_arrow_fork():
    # tests/ excluded: fixtures may quote historical text.
    checked = []
    for dirpath, _dirnames, filenames in os.walk(ROOT):
        if ".git" in dirpath or "/tests" in dirpath:
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


def test_profile_to_protocol_mapping_documented():
    # The code-review profile's Phase 0/10/11 agents must be mapped onto the
    # protocol stages, or a reader cannot tell Inject==Alpha from the diagram.
    for needle in ("Inject = Alpha (Phase 0)", "Extract = Omega (Phase 10)",
                   "Validate = Lambda (Phase 11)"):
        assert needle in _read("SKILL.md"), f"SKILL.md must state {needle!r}"
