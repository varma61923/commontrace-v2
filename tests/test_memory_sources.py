"""commontrace/memory_sources.py: causal measurement of memory that lives in
a plain file (CLAUDE.md/AGENTS.md-shaped), through the same holdout path
`commontrace/measure.py`'s tests already hold to the same standard --
the important test here is end-to-end: a fixture file where one section
genuinely raises the task success rate and another does nothing, rendered
occasion by occasion, then read back through `commontrace experiment`'s own
analysis path. If that path cannot tell the two apart, nothing else here
matters.
"""
from __future__ import annotations

import json
import os
import random

from commontrace import experiment, holdout_io, memory_sources
from commontrace.commands import experiment_cmd

# Fixed for the same reason tests/test_measure.py fixes it: holdout_io.configure
# derives a fresh salt from the clock on every call, which re-randomizes which
# occasions land in which arm on every test run. These tests check that the
# pipeline is wired correctly, not how often the statistics err.
FIXED_SALT = "test-memory-sources-fixed-salt"

FIXTURE = """# Project notes

Read this before doing anything.

## Idempotency

Always set an idempotency key on webhook handlers.

## Office hours

The office is closed on Fridays.

## Deploys

Deploy only from main.
"""


def _configure(root, rate):
    holdout_io.configure(str(root), rate=rate)
    path = holdout_io.config_path(str(root))
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)
    config["salt"] = FIXED_SALT
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh)


def _write(tmp_path, content=FIXTURE, name="CLAUDE.md"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def _analyze(root):
    rows, _rate, _corrupt = experiment_cmd._load(str(root))
    rows, _salt, _excluded = experiment_cmd.scope_to_current_salt(str(root), rows)
    return {e.lesson_slug: e for e in experiment.analyze(experiment_cmd._observations(rows))}


class TestParsing:
    def test_sections_split_on_h2_headings(self, tmp_path):
        preamble, sections = memory_sources.parse_sections(FIXTURE)
        assert "Read this before doing anything" in preamble
        assert [s.id for s in sections] == ["idempotency", "office-hours", "deploys"]
        assert [s.heading for s in sections] == ["Idempotency", "Office hours", "Deploys"]

    def test_preamble_is_never_a_section(self, tmp_path):
        _preamble, sections = memory_sources.parse_sections(FIXTURE)
        assert all("Read this before" not in s.text for s in sections)

    def test_section_text_reproduces_the_original_when_all_are_kept(self, tmp_path):
        preamble, sections = memory_sources.parse_sections(FIXTURE)
        assert preamble + "".join(s.text for s in sections) == FIXTURE

    def test_no_headings_means_one_indivisible_preamble(self, tmp_path):
        preamble, sections = memory_sources.parse_sections("just plain text\nno headings\n")
        assert sections == []
        assert preamble == "just plain text\nno headings\n"

    def test_duplicate_headings_get_stable_suffixed_ids(self, tmp_path):
        content = "## Notes\nfirst\n## Notes\nsecond\n"
        _preamble, sections = memory_sources.parse_sections(content)
        assert [s.id for s in sections] == ["notes", "notes-2"]

    def test_a_heading_with_no_alnum_chars_falls_back_to_section(self, tmp_path):
        content = "## !!!\nbody\n"
        _preamble, sections = memory_sources.parse_sections(content)
        assert sections[0].id == "section"


class TestEndToEnd:
    def test_a_section_that_helps_is_found_and_one_that_does_not_is_not(self, tmp_path):
        _configure(tmp_path, 0.5)
        path = _write(tmp_path)
        source = memory_sources.FileMemorySource(path, root=str(tmp_path))
        rng = random.Random(1234)

        for i in range(400):
            occasion = f"task-{i}"
            result = source.render(occasion)
            delivered_idempotency = "idempotency" not in result.withheld
            # Ground truth this test plants: only "idempotency" changes outcomes.
            p = 0.8 if delivered_idempotency else 0.4
            source.record_outcome(occasion, succeeded=rng.random() < p)

        effects = _analyze(tmp_path)
        assert effects["idempotency"].verdict == experiment.VERDICT_HELPS
        assert effects["idempotency"].effect > 0.2
        assert effects["office-hours"].verdict != experiment.VERDICT_HELPS

    def test_render_is_deterministic_for_the_same_occasion(self, tmp_path):
        _configure(tmp_path, 0.5)
        path = _write(tmp_path)
        source = memory_sources.FileMemorySource(path, root=str(tmp_path))
        first = source.render("o1")
        second = source.render("o1")
        assert first == second

    def test_a_stopped_experiment_renders_everything(self, tmp_path):
        _configure(tmp_path, 0.0)
        path = _write(tmp_path)
        source = memory_sources.FileMemorySource(path, root=str(tmp_path))
        result = source.render("o1")
        assert result.text == FIXTURE
        assert result.withheld == []

    def test_withheld_section_is_actually_absent_from_the_rendered_text(self, tmp_path):
        _configure(tmp_path, 0.99)
        path = _write(tmp_path)
        source = memory_sources.FileMemorySource(path, root=str(tmp_path))
        result = source.render("o1")
        for section_id in result.withheld:
            heading = {"idempotency": "Idempotency", "office-hours": "Office hours",
                       "deploys": "Deploys"}[section_id]
            assert f"## {heading}" not in result.text


class TestBlocklist:
    def test_withdrawn_section_is_never_delivered_and_never_logged(self, tmp_path):
        _configure(tmp_path, 0.0)  # rate 0 would deliver everything if not blocked
        path = _write(tmp_path)
        memory_sources.withdraw(str(tmp_path), path, "office-hours")
        source = memory_sources.FileMemorySource(path, root=str(tmp_path))
        result = source.render("o1")
        assert "Office hours" not in result.text
        assert result.blocked == ["office-hours"]
        log_path = holdout_io.holdout_log_path(str(tmp_path))
        if os.path.isfile(log_path):
            with open(log_path, encoding="utf-8") as fh:
                logged = {json.loads(line)["lesson"] for line in fh if line.strip()}
            assert "office-hours" not in logged

    def test_reinstate_undoes_withdraw(self, tmp_path):
        path = _write(tmp_path)
        memory_sources.withdraw(str(tmp_path), path, "office-hours")
        assert memory_sources.reinstate(str(tmp_path), path, "office-hours") is True
        assert memory_sources.blocked_ids(str(tmp_path), path) == set()

    def test_reinstating_something_never_blocked_reports_false(self, tmp_path):
        path = _write(tmp_path)
        assert memory_sources.reinstate(str(tmp_path), path, "office-hours") is False

    def test_withdraw_is_idempotent(self, tmp_path):
        path = _write(tmp_path)
        memory_sources.withdraw(str(tmp_path), path, "office-hours")
        memory_sources.withdraw(str(tmp_path), path, "office-hours")
        assert memory_sources.blocked_ids(str(tmp_path), path) == {"office-hours"}

    def test_blocklist_is_keyed_per_file(self, tmp_path):
        path_a = _write(tmp_path, name="CLAUDE.md")
        path_b = _write(tmp_path, name="AGENTS.md")
        memory_sources.withdraw(str(tmp_path), path_a, "office-hours")
        assert memory_sources.blocked_ids(str(tmp_path), path_a) == {"office-hours"}
        assert memory_sources.blocked_ids(str(tmp_path), path_b) == set()

    def test_a_source_with_everything_blocked_renders_only_the_preamble(self, tmp_path):
        path = _write(tmp_path)
        for section_id in ("idempotency", "office-hours", "deploys"):
            memory_sources.withdraw(str(tmp_path), path, section_id)
        source = memory_sources.FileMemorySource(path, root=str(tmp_path))
        result = source.render("o1")
        assert result.text.strip() == "# Project notes\n\nRead this before doing anything.".strip()
        assert set(result.blocked) == {"idempotency", "office-hours", "deploys"}


class TestOutcomes:
    def test_record_outcome_reaches_the_shared_log(self, tmp_path):
        path = _write(tmp_path)
        source = memory_sources.FileMemorySource(path, root=str(tmp_path))
        assert source.record_outcome("o1", succeeded=True) is True
        assert holdout_io.read_outcomes(str(tmp_path)) == {"o1": True}


class TestRootResolution:
    def test_a_relative_source_path_outside_root_falls_back_to_absolute(self, tmp_path):
        other_dir = tmp_path / "elsewhere"
        other_dir.mkdir()
        outside_path = other_dir / "CLAUDE.md"
        outside_path.write_text(FIXTURE, encoding="utf-8")
        root_dir = tmp_path / "store"
        root_dir.mkdir()
        key = memory_sources._source_key(str(root_dir), str(outside_path))
        assert key == str(outside_path.resolve())

    def test_a_source_path_inside_root_is_stored_relative(self, tmp_path):
        path = _write(tmp_path)
        key = memory_sources._source_key(str(tmp_path), path)
        assert key == "CLAUDE.md"
