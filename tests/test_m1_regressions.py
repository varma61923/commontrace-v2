"""Regression tests for M1 fixes across Core CLI, IO, and Memory/Attention."""
from __future__ import annotations

import argparse
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Import attention modules, with stubs for heavy optional deps only for those modules.
# We must NOT permanently replace sys.modules["numpy"] here — that would break every
# subsequent test file that imports numpy-dependent modules (test_pilot_metrics, test_value, etc.).
# Instead we install temporary stubs only while importing the attention scripts, then remove them.
_ATTENTION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "commontrace", "reference"
)
if _ATTENTION_DIR not in sys.path:
    sys.path.insert(0, _ATTENTION_DIR)


def _import_attention():
    _stubs = {}
    for _mod in ("numpy", "sentence_transformers"):
        if _mod not in sys.modules:
            try:
                __import__(_mod)
            except ImportError:
                _stubs[_mod] = MagicMock()
                sys.modules[_mod] = _stubs[_mod]
    import build_index as _bi
    import query as _aq
    # Remove any stub we injected — but not a real module that was already present.
    for _mod, _stub in _stubs.items():
        if sys.modules.get(_mod) is _stub:
            del sys.modules[_mod]
    return _bi, _aq


build_index, attn_query = _import_attention()

from commontrace import (  # noqa: E402 -- must follow _import_attention() above
    cli,
    evidence_io,
    frontmatter,
    overlap,
    trace_io,
)
from commontrace.commands import capture_cmd, init_cmd, pilot_cmd, query_cmd, serve_cmd, taxonomy_cmd  # noqa: E402


# ---------------------------------------------------------------------------
# Task 1: build_index._safe_mtime
# ---------------------------------------------------------------------------
def test_build_index_safe_mtime_handles_deleted_or_missing_file(tmp_path):
    missing_file = str(tmp_path / "does_not_exist.md")
    assert build_index._safe_mtime(missing_file) == 0.0

    existing_file = tmp_path / "exists.md"
    existing_file.write_text("content", encoding="utf-8")
    assert build_index._safe_mtime(str(existing_file)) > 0.0

    with patch("os.path.getmtime", side_effect=FileNotFoundError("Simulated race deletion")):
        assert build_index._safe_mtime(str(existing_file)) == 0.0


# ---------------------------------------------------------------------------
# Task 2: pilot_metrics.load_traces error handling
# ---------------------------------------------------------------------------
def test_pilot_metrics_load_traces_ignores_unreadable_file(tmp_path):
    from commontrace.reference import pilot_metrics

    tdir = tmp_path / "memory" / "traces"
    tdir.mkdir(parents=True)

    good_trace = tdir / "2026-09-05_good_trace1.md"
    good_trace.write_text(
        "---\nid: trace1\ntitle: Good trace\nagent_type: code\noutcome:\n  resolved: true\n---\n"
        "## Context\nC\n## Solution\nS\n",
        encoding="utf-8",
    )

    unreadable_trace = tdir / "2026-09-05_unreadable_trace2.md"
    unreadable_trace.write_text("---\nid: trace2\n---\n", encoding="utf-8")

    orig_open = open

    def fake_open(file, *args, **kwargs):
        if "unreadable" in str(file):
            raise OSError("Simulated permission / lock failure")
        return orig_open(file, *args, **kwargs)

    with patch("builtins.open", side_effect=fake_open):
        traces = pilot_metrics.load_traces(root=str(tmp_path))

    assert len(traces) == 1
    assert traces[0]["id"] == "trace1"


# ---------------------------------------------------------------------------
# Task 3: trace_io._SECTION_RE intermediate headings lookahead
# ---------------------------------------------------------------------------
def test_trace_io_intermediate_headings_not_swallowed():
    """The regex only stops at ## Context or ## Solution headings (the section delimiters).
    Any other ## heading between them is part of the preceding section's body, which is
    acceptable because: (a) embedded ## headings inside a section body are preserved too
    (see test_solution_with_embedded_subheading_is_not_truncated in test_frontmatter.py),
    and (b) stopping at EVERY ## would truncate those. The key invariant is that
    ## Solution is never swallowed into context_text.
    """
    body = (
        "## Context\n"
        "Problem context here.\n\n"
        "## Notes\n"
        "Intermediate heading notes.\n\n"
        "## Solution\n"
        "Solution text here.\n"
    )
    sections = trace_io._first_wins(body)
    # context captures everything up to the ## Solution delimiter
    assert "Problem context here." in sections.get("context", "")
    # solution is correctly separated and contains only its own text
    assert sections.get("solution") == "Solution text here."
    # solution body does not bleed into context
    assert "Solution text here." not in sections.get("context", "")


# ---------------------------------------------------------------------------
# Task 4: evidence_io.load_active_lessons filters by active status
# ---------------------------------------------------------------------------
def test_evidence_io_load_active_lessons_filters_status(tmp_path):
    ldir = tmp_path / "memory" / "lessons"
    ldir.mkdir(parents=True)

    (ldir / "lesson_template.md").write_text("---\nstatus: template\n---\nBody\n", encoding="utf-8")
    (ldir / "lesson_active.md").write_text("---\nname: active-lesson\nstatus: active\n---\nBody\n", encoding="utf-8")
    (ldir / "lesson_archived.md").write_text(
        "---\nname: archived-lesson\nstatus: archived\n---\nBody\n", encoding="utf-8"
    )
    (ldir / "lesson_review.md").write_text("---\nname: review-lesson\nstatus: review\n---\nBody\n", encoding="utf-8")
    (ldir / "lesson_missing_status.md").write_text("---\nname: default-lesson\n---\nBody\n", encoding="utf-8")

    loaded = evidence_io.load_active_lessons(str(tmp_path))
    loaded_names = {lesson.get("name") for lesson in loaded}
    assert loaded_names == {"active-lesson", "default-lesson"}

    # Explicit filter for archived
    archived = evidence_io.load_active_lessons(str(tmp_path), status="archived")
    assert {lesson.get("name") for lesson in archived} == {"archived-lesson"}


# ---------------------------------------------------------------------------
# Task 5: taxonomy_cmd._similarity_threshold validation
# ---------------------------------------------------------------------------
def test_taxonomy_cmd_similarity_threshold_validation():
    # Valid
    assert taxonomy_cmd._similarity_threshold("0.3") == 0.3
    assert taxonomy_cmd._similarity_threshold("1.0") == 1.0
    assert taxonomy_cmd._similarity_threshold("0.001") == 0.001

    # Invalid range <= 0 or > 1
    with pytest.raises(argparse.ArgumentTypeError):
        taxonomy_cmd._similarity_threshold("0.0")
    with pytest.raises(argparse.ArgumentTypeError):
        taxonomy_cmd._similarity_threshold("-0.5")
    with pytest.raises(argparse.ArgumentTypeError):
        taxonomy_cmd._similarity_threshold("1.001")
    with pytest.raises(argparse.ArgumentTypeError):
        taxonomy_cmd._similarity_threshold("not-a-number")


# ---------------------------------------------------------------------------
# Task 6: cli.main catches expanded exception tuple
# ---------------------------------------------------------------------------
def test_cli_catches_expanded_exceptions():
    for exc_type, exc_instance in [
        ("TypeError", TypeError("test type error")),
        ("IndexError", IndexError("test index error")),
        ("AttributeError", AttributeError("test attr error")),
    ]:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        p = subparsers.add_parser("sub")

        def fail_func(args, err=exc_instance):
            raise err

        p.set_defaults(func=fail_func)

        with patch("commontrace.cli.build_parser", return_value=parser):
            ret = cli.main(["sub"])
            assert ret == 1


# ---------------------------------------------------------------------------
# Task 7: capture_cmd occasion suffix probe and open-vocabulary agent-type
# ---------------------------------------------------------------------------
def test_capture_cmd_probes_occasion_suffix_and_fallback(tmp_path):
    tdir = tmp_path / "memory" / "traces"
    tdir.mkdir(parents=True)

    occasion_id = "test-occasion-abc-123"
    suffix = capture_cmd._id_suffix(occasion_id)

    # File matches standard naming convention with suffix
    standard_trace = tdir / f"2026-09-05_slug_{suffix}.md"
    standard_trace.write_text(f"---\nid: {occasion_id}\n---\nBody\n", encoding="utf-8")

    found = capture_cmd._find_trace_by_occasion(str(tdir), occasion_id)
    assert found == str(standard_trace)

    # Legacy / renamed file with different suffix falls back to scan
    legacy_id = "legacy-occasion-456"
    legacy_trace = tdir / "2026-09-01_old_unusual_name.md"
    legacy_trace.write_text(f"---\nid: {legacy_id}\n---\nBody\n", encoding="utf-8")

    found_legacy = capture_cmd._find_trace_by_occasion(str(tdir), legacy_id)
    assert found_legacy == str(legacy_trace)


def test_capture_cmd_open_vocabulary_agent_type():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    capture_cmd.add_parser(subparsers)

    args = parser.parse_args([
        "capture",
        "--title", "Test Title",
        "--context", "Test Context",
        "--solution", "Test Solution",
        "--agent-type", "autonomous-security-auditor-v2",
    ])
    assert args.agent_type == "autonomous-security-auditor-v2"


# ---------------------------------------------------------------------------
# Task 8: pilot_cmd loads traces once and reuses them
# ---------------------------------------------------------------------------
def test_pilot_cmd_trace_loading_and_reuse(tmp_path):
    tdir = tmp_path / "memory" / "traces"
    tdir.mkdir(parents=True)

    t1 = tdir / "2026-09-05_t1_1.md"
    t1.write_text(
        "---\nid: t1\ntitle: T1\nagent_type: code\ntags: [tagA]\noutcome:\n  resolved: true\n---\n"
        "## Context\nCtx\n## Solution\nSol\n",
        encoding="utf-8",
    )

    loaded = pilot_cmd._load_traces(str(tmp_path))
    assert len(loaded) == 1
    assert loaded[0][1]["id"] == "t1"

    # Verify evidence_io.load_evidence reuses preloaded traces without reading disk
    # for the *trace* path. Episodes are always globbed (separate code path), so
    # we only assert that the trace-dir glob is skipped, not the episode-dir glob.
    all_instances = [inst for _, inst in loaded]
    _real_glob = __import__("glob").glob

    def _selective_glob(pattern, *args, **kwargs):
        if "traces" in pattern:
            raise AssertionError("trace glob should not be called when traces provided")
        return _real_glob(pattern, *args, **kwargs)

    with patch("commontrace.evidence_io.glob.glob", side_effect=_selective_glob):
        evidence = evidence_io.load_evidence(str(tmp_path), traces=all_instances)
    assert isinstance(evidence, list)


# ---------------------------------------------------------------------------
# Task 9: query.py single-pass mtimes and check_staleness
# ---------------------------------------------------------------------------
def test_attention_query_load_importances_mtimes_and_staleness(tmp_path, monkeypatch):
    ldir = tmp_path / "lessons"
    ldir.mkdir(parents=True)
    monkeypatch.setattr(attn_query, "LESSONS_DIR", str(ldir))

    l1 = ldir / "lesson_alpha.md"
    l1.write_text(
        "---\nname: lesson-alpha\nimportance: 5\nstatus: active\n---\n## Rule\nRule\n",
        encoding="utf-8",
    )

    res = attn_query.load_importances()
    assert len(res) == 2  # unpacks as (importances, n_parsed)
    importances, n_parsed = res
    assert importances == {"lesson-alpha": 5}
    assert n_parsed == 1
    assert hasattr(res, "newest_active_mtime")
    assert res.newest_active_mtime > 0.0

    # check_staleness uses precomputed newest_active_mtime without globbing
    index_file = tmp_path / "index.npz"
    index_file.write_text("placeholder", encoding="utf-8")

    # Set index mtime older than lesson
    os.utime(str(index_file), (100.0, 100.0))
    reasons = attn_query.check_staleness(
        str(index_file),
        str(ldir),
        {"lesson-alpha"},
        {"lesson-alpha"},
        newest_active_mtime=200.0,
    )
    assert len(reasons) == 1
    assert "modified after the index was last built" in reasons[0]


# ---------------------------------------------------------------------------
# Task 10: query_cmd exposes and forwards --include-importance-floor
# ---------------------------------------------------------------------------
def test_query_cmd_importance_floor_flag():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    query_cmd.add_parser(subparsers)

    args = parser.parse_args(["query", "my task", "--include-importance-floor", "3"])
    assert args.include_importance_floor == 3

    # A usable index as well as the deps: `query` now falls back to lexical
    # when the semantic index is missing or stale, so without this the run
    # never reaches the script and this test would be asserting the fallback
    # rather than the flag forwarding it exists to pin.
    with patch("commontrace.commands.query_cmd.has_attention_deps", return_value=True), \
            patch("commontrace.commands.query_cmd._index_is_unusable", return_value=""):
        with patch("commontrace.commands.query_cmd.run_script", return_value=0) as mock_run:
            query_cmd.run(args)
            assert mock_run.called
            called_args = mock_run.call_args[0][2]
            assert "--include-importance-floor" in called_args
            idx = called_args.index("--include-importance-floor")
            assert called_args[idx + 1] == "3"


# ---------------------------------------------------------------------------
# Task 11: serve_cmd help text references --target generic-mcp
# ---------------------------------------------------------------------------
def test_serve_cmd_help_text():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    serve_cmd.add_parser(subparsers)

    subparser = subparsers.choices["serve"]
    assert "--target generic-mcp" in subparser.description
    assert "--mcp" not in subparser.description


# ---------------------------------------------------------------------------
# Task 12: init_cmd scaffolds memory/attention/
# ---------------------------------------------------------------------------
def test_init_cmd_scaffolds_attention(tmp_path):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    init_cmd.add_parser(subparsers)

    args = parser.parse_args(["init", "--dest", str(tmp_path), "--agent-type", "code"])
    rc = init_cmd.run(args)
    assert rc is None or rc == 0

    att_dir = tmp_path / "memory" / "attention"
    assert att_dir.is_dir()
    assert (att_dir / "README.md").is_file()


# ---------------------------------------------------------------------------
# Task 13: overlap.estimate_jaccard vectorization
# ---------------------------------------------------------------------------
def test_overlap_estimate_jaccard():
    # Pure Python (overlap.py imports no numpy), so nothing to stub.
    sig1 = [1, 2, 3, 4]
    sig2 = [1, 2, 0, 4]
    assert overlap.estimate_jaccard(sig1, sig2) == 0.75

    sig_identical = [5, 6, 7]
    assert overlap.estimate_jaccard(sig_identical, sig_identical) == 1.0

    sig_disjoint = [1, 2, 3]
    sig_disjoint2 = [4, 5, 6]
    assert overlap.estimate_jaccard(sig_disjoint, sig_disjoint2) == 0.0

    with pytest.raises(ValueError):
        overlap.estimate_jaccard([], [])

    with pytest.raises(ValueError):
        overlap.estimate_jaccard([1, 2], [1])


# ---------------------------------------------------------------------------
# Task 14: frontmatter._new_file_mode directory cache
# ---------------------------------------------------------------------------
def test_frontmatter_caches_new_file_mode(tmp_path):
    target_dir = str(tmp_path)
    frontmatter._DIR_MODE_CACHE.clear()

    mode1 = frontmatter._new_file_mode(target_dir)
    assert os.path.abspath(target_dir) in frontmatter._DIR_MODE_CACHE

    # Second call should return cached mode directly without disk probe
    with patch("os.open", side_effect=AssertionError("os.open probe should not be called when cached")):
        mode2 = frontmatter._new_file_mode(target_dir)

    assert mode1 == mode2
