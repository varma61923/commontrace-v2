from __future__ import annotations

import os

import pytest

from commontrace import failure_signals, paths
from commontrace.cli import main


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    return tmp_path


def _capture(store, title, context, solution, *, resolved=None, repeated_error=None,
             agent_id=None, created_at=None, tags="t1"):
    argv = [
        "capture", "--title", title, "--context", context, "--solution", solution,
        "--tags", tags, "--agent-type", "support", "--dest", str(store),
    ]
    if resolved is True:
        argv.append("--resolved")
    elif resolved is False:
        argv.append("--not-resolved")
    if repeated_error is True:
        argv.append("--repeated-error")
    elif repeated_error is False:
        argv.append("--not-repeated-error")
    if agent_id:
        argv += ["--agent-id", agent_id]
    assert main(argv) == 0

    if created_at is not None:
        from commontrace import frontmatter
        tdir = paths.traces_dir(str(store))
        newest = max(
            (os.path.join(tdir, f) for f in os.listdir(tdir) if f != "README.md"),
            key=os.path.getmtime,
        )
        inst, body = frontmatter.read(newest)
        inst["created_at"] = created_at
        frontmatter.write(newest, inst, body)


class TestLoadFailureOccurrences:
    def test_an_unresolved_trace_is_a_failure(self, store):
        _capture(store, "t", "c", "s", resolved=False)
        occ = failure_signals.load_failure_occurrences(str(store))
        assert len(occ) == 1

    def test_a_repeated_error_trace_is_a_failure_even_if_resolved_is_unset(self, store):
        _capture(store, "t", "c", "s", repeated_error=True)
        occ = failure_signals.load_failure_occurrences(str(store))
        assert len(occ) == 1

    def test_a_resolved_trace_is_not_a_failure(self, store):
        _capture(store, "t", "c", "s", resolved=True)
        assert failure_signals.load_failure_occurrences(str(store)) == []

    def test_a_trace_with_no_outcome_recorded_is_not_a_failure(self, store):
        _capture(store, "t", "c", "s")
        assert failure_signals.load_failure_occurrences(str(store)) == []

    def test_agent_type_filter_is_honoured(self, store):
        _capture(store, "t", "c", "s", resolved=False)
        assert failure_signals.load_failure_occurrences(str(store), agent_type="code") == []
        assert len(failure_signals.load_failure_occurrences(str(store), agent_type="support")) == 1


class TestBuildSignals:
    def test_repeated_failures_cluster_into_one_signal(self, store):
        for i in range(3):
            _capture(
                store, f"Export timeout {i}",
                "customer export job timed out waiting on the worker queue",
                "increase the worker timeout and retry",
                resolved=False,
            )
        signals, by_id = failure_signals.build_signals(str(store))
        assert len(signals) == 1
        assert signals[0].size == 3
        assert set(signals[0].trace_ids) <= set(by_id)

    def test_unrelated_single_failures_do_not_cluster(self, store):
        _capture(store, "Export timeout", "export job timed out", "retry", resolved=False)
        _capture(store, "Login broken", "user cannot log in at all", "reset password", resolved=False)
        signals, _by_id = failure_signals.build_signals(str(store))
        assert signals == []

    def test_affected_agents_are_collected_and_deduplicated(self, store):
        for i in range(2):
            _capture(
                store, f"Export timeout {i}", "export job timed out waiting on queue",
                "retry", resolved=False, agent_id="agent-1",
            )
        _capture(
            store, "Export timeout 2", "export job timed out waiting on queue",
            "retry", resolved=False, agent_id="agent-2",
        )
        signals, _by_id = failure_signals.build_signals(str(store))
        assert signals[0].affected_agents == ["agent-1", "agent-2"]

    def test_no_recorded_agent_id_reports_no_affected_agents(self, store):
        for i in range(2):
            _capture(store, f"Export timeout {i}", "export job timed out queue", "retry", resolved=False)
        signals, _by_id = failure_signals.build_signals(str(store))
        assert signals[0].affected_agents == []


class TestTrend:
    def test_too_few_dated_occurrences_is_unknown(self):
        assert failure_signals._trend(["2026-01-01"]) == "unknown"
        assert failure_signals._trend([]) == "unknown"

    def test_a_clear_increase_is_reported(self):
        dated = ["2026-01-01", "2026-01-02"] + [f"2026-02-{i:02d}" for i in range(1, 8)]
        assert failure_signals._trend(dated) == "increasing"

    def test_a_clear_decrease_is_reported(self):
        dated = [f"2026-01-{i:02d}" for i in range(1, 8)] + ["2026-02-01", "2026-02-02"]
        assert failure_signals._trend(dated) == "decreasing"

    def test_a_flat_rate_is_steady(self):
        dated = [f"2026-01-{i:02d}" for i in range(1, 5)] + [f"2026-02-{i:02d}" for i in range(1, 5)]
        assert failure_signals._trend(dated) == "steady"


class TestExportFormats:
    def _signal_and_by_id(self, store):
        for i in range(2):
            _capture(
                store, f"Export timeout {i}", "export job timed out waiting on queue",
                "increase the worker timeout", resolved=False, tags="timeout,queue",
            )
        signals, by_id = failure_signals.build_signals(str(store))
        return signals[0], by_id

    def test_langsmith_shape_matches_what_the_importer_reads(self, store):
        signal, by_id = self._signal_and_by_id(store)
        rows = failure_signals.export_langsmith(signal, by_id)
        assert len(rows) == 2
        for row in rows:
            assert set(row) == {"inputs", "outputs", "metadata"}
            assert row["inputs"]["input"]
            assert row["outputs"]["output"]
            assert row["metadata"]["commontrace_signal"] == signal.name

    def test_braintrust_shape_matches_what_the_importer_reads(self, store):
        signal, by_id = self._signal_and_by_id(store)
        rows = failure_signals.export_braintrust(signal, by_id)
        assert len(rows) == 2
        for row in rows:
            assert set(row) == {"input", "expected", "metadata"}
            assert row["input"]
            assert row["expected"]


class TestSignalsCli:
    def test_list_reports_nothing_for_an_empty_store(self, store, capsys):
        assert main(["signals", "list", "--dest", str(store)]) == 0
        assert "no failure signals found" in capsys.readouterr().out

    def test_list_reports_a_clustered_signal(self, store, capsys):
        for i in range(2):
            _capture(store, f"Export timeout {i}", "export job timed out waiting on queue",
                     "retry", resolved=False)
        assert main(["signals", "list", "--dest", str(store)]) == 0
        out = capsys.readouterr().out
        assert "size=2" in out

    def test_export_to_stdout(self, store, capsys):
        for i in range(2):
            _capture(store, f"Export timeout {i}", "export job timed out waiting on queue",
                     "retry", resolved=False)
        signals, _ = failure_signals.build_signals(str(store))
        name = signals[0].name
        capsys.readouterr()
        assert main([
            "signals", "export", name, "--format", "langsmith", "--dest", str(store),
        ]) == 0
        out = capsys.readouterr().out
        assert out.strip().count("\n") == 1

    def test_export_an_unknown_signal_name_fails_cleanly(self, store, capsys):
        rc = main(["signals", "export", "nope", "--format", "braintrust", "--dest", str(store)])
        assert rc == 1
        assert "no signal named" in capsys.readouterr().err


class TestDistillScopedToFailures:
    REFUND = "customer confused about refund timeline contradictory docs"

    def _seed(self, store):
        for n in (1, 2):
            _capture(store, f"Refund confusion {n}", self.REFUND, "link the policy", resolved=False)
        for n in (1, 2):
            _capture(store, f"Refund handled {n}", self.REFUND, "link the policy", resolved=True)

    def _candidates(self, store):
        ldir = paths.lessons_dir(str(store))
        return [f for f in os.listdir(ldir) if f.startswith("lesson_") and "template" not in f]

    def test_failed_only_clusters_the_failures(self, store, capsys):
        self._seed(store)
        assert main(["distill", "--failed", "--dest", str(store)]) == 0
        out = capsys.readouterr().out
        assert "2 trace(s) considered" in out
        assert len(self._candidates(store)) == 1

    def test_without_the_flag_everything_is_considered(self, store, capsys):
        self._seed(store)
        assert main(["distill", "--dest", str(store)]) == 0
        assert "4 trace(s) considered" in capsys.readouterr().out

    def test_no_failures_says_so_and_writes_nothing(self, store, capsys):
        for n in (1, 2):
            _capture(store, f"ok {n}", self.REFUND, "s", resolved=True)
        assert main(["distill", "--failed", "--dest", str(store)]) == 0
        assert "no failed traces" in capsys.readouterr().out
        assert self._candidates(store) == []

    def test_one_named_signal(self, store, capsys):
        self._seed(store)
        signals, _ = failure_signals.build_signals(str(store))
        assert len(signals) == 1
        assert main(["distill", "--signal", signals[0].name, "--dest", str(store)]) == 0
        assert "2 trace(s) considered" in capsys.readouterr().out

    def test_an_unknown_signal_is_refused_naming_the_real_ones(self, store, capsys):
        self._seed(store)
        assert main(["distill", "--signal", "nope", "--dest", str(store)]) == 2
        err = capsys.readouterr().err
        assert "no failure signal named 'nope'" in err and "signals:" in err
