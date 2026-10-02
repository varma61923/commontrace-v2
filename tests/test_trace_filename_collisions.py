import json
import os

from commontrace import paths
from commontrace.cli import main
from commontrace.commands import capture_cmd


def _capture(dest, title, occasion_id, context="c", solution="s", extra=()):
    return main([
        "capture", "--title", title, "--context", context, "--solution", solution,
        "--occasion-id", occasion_id, "--dest", str(dest), *extra,
    ])


def _trace_ids(dest):
    ids = set()
    for name in os.listdir(paths.traces_dir(str(dest))):
        if not name.endswith(".md") or name == "README.md":
            continue
        with open(os.path.join(paths.traces_dir(str(dest)), name), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("id:"):
                    ids.add(line.split(":", 1)[1].strip())
                    break
    return ids


class TestTheSuffixIsInjective:
    def test_ids_sharing_a_long_prefix_get_different_suffixes(self):
        a = capture_cmd._id_suffix("JIRA-ROBOTICS-PLATFORM-4711")
        b = capture_cmd._id_suffix("JIRA-ROBOTICS-PLATFORM-4712")
        assert a != b

    def test_short_ids_are_unchanged(self):
        assert capture_cmd._id_suffix("TICKET-42") == "TICKET-42"

    def test_it_stays_deterministic(self):
        assert capture_cmd._id_suffix("JIRA-ROBOTICS-PLATFORM-4711") == \
            capture_cmd._id_suffix("JIRA-ROBOTICS-PLATFORM-4711")

    def test_it_stays_filesystem_safe(self):
        suffix = capture_cmd._id_suffix("../../etc/passwd and a space/../..")
        assert "/" not in suffix and " " not in suffix and ".." not in suffix

    def test_an_id_that_sanitizes_to_nothing_still_yields_a_name(self):
        assert capture_cmd._id_suffix("///") == "trace"


class TestNoCaptureIsLost:
    def test_five_ticket_ids_sharing_a_prefix_produce_five_traces(self, tmp_path):
        assert main(["init", "--dest", str(tmp_path)]) == 0
        ids = [f"JIRA-ROBOTICS-PLATFORM-471{i}" for i in range(5)]
        for i, occ in enumerate(ids):
            assert _capture(tmp_path, "Sensor jitter after firmware update", occ,
                            context=f"ctx {i}", solution=f"sol {i}") == 0

        assert _trace_ids(tmp_path) == set(ids)

    def test_each_trace_keeps_its_own_content(self, tmp_path):
        assert main(["init", "--dest", str(tmp_path)]) == 0
        for i in range(3):
            assert _capture(tmp_path, "Same title every time",
                            f"RUN-2026-09-06-PIPELINE-{i}",
                            context=f"context number {i}",
                            solution=f"solution number {i}") == 0

        bodies = []
        tdir = paths.traces_dir(str(tmp_path))
        for name in sorted(os.listdir(tdir)):
            if name.endswith(".md") and name != "README.md":
                bodies.append(open(os.path.join(tdir, name), encoding="utf-8").read())
        assert len(bodies) == 3
        for i in range(3):
            assert any(f"context number {i}" in b for b in bodies), f"lost context {i}"

    def test_recapturing_one_occasion_still_merges_rather_than_duplicating(self, tmp_path):
        assert main(["init", "--dest", str(tmp_path)]) == 0
        occ = "JIRA-ROBOTICS-PLATFORM-4711"
        assert _capture(tmp_path, "Arm drift", occ) == 0
        assert _capture(tmp_path, "Arm drift", occ, extra=["--resolved"]) == 0

        assert _trace_ids(tmp_path) == {occ}
        tdir = paths.traces_dir(str(tmp_path))
        files = [n for n in os.listdir(tdir) if n.endswith(".md") and n != "README.md"]
        assert len(files) == 1
        assert "resolved: true" in open(os.path.join(tdir, files[0]), encoding="utf-8").read()

    def test_the_occasions_remain_joinable_to_their_arms(self, tmp_path):
        assert main(["init", "--dest", str(tmp_path)]) == 0
        ids = [f"JIRA-ROBOTICS-PLATFORM-471{i}" for i in range(4)]
        for occ in ids:
            assert _capture(tmp_path, "Sensor jitter", occ, extra=["--resolved"]) == 0

        from commontrace.commands import experiment_cmd
        outcomes = experiment_cmd._outcomes_by_occasion(str(tmp_path))
        for occ in ids:
            assert outcomes.get(occ) is True, f"{occ} cannot be joined to its arm"


class TestDoctorReportsAlreadyDamagedStores:
    def test_a_healthy_store_reports_none(self, tmp_path, capsys):
        assert main(["init", "--dest", str(tmp_path)]) == 0
        for i in range(3):
            _capture(tmp_path, "T", f"JIRA-ROBOTICS-PLATFORM-471{i}")
        capsys.readouterr()
        main(["doctor", "--dest", str(tmp_path)])
        assert "[OK  ] trace filename collisions" in capsys.readouterr().out

    def test_a_store_that_already_lost_a_trace_is_flagged(self, tmp_path, capsys):
        assert main(["init", "--dest", str(tmp_path)]) == 0
        log = os.path.join(paths.memory_dir(str(tmp_path)), "holdout_log.jsonl")
        with open(log, "w", encoding="utf-8") as fh:
            for occ in ("JIRA-ROBOTICS-PLATFORM-4711", "JIRA-ROBOTICS-PLATFORM-4712"):
                fh.write(json.dumps({"occasion_id": occ, "lesson": "a", "injected": True,
                                     "rate": 0.5, "salt": "s"}) + "\n")
        _capture(tmp_path, "Sensor jitter", "JIRA-ROBOTICS-PLATFORM-4712")

        capsys.readouterr()
        main(["doctor", "--dest", str(tmp_path)])
        assert "[WARN] trace filename collisions" in capsys.readouterr().out

    def test_a_prefix_pair_with_neither_captured_is_not_flagged(self, tmp_path, capsys):
        assert main(["init", "--dest", str(tmp_path)]) == 0
        log = os.path.join(paths.memory_dir(str(tmp_path)), "holdout_log.jsonl")
        with open(log, "w", encoding="utf-8") as fh:
            for occ in ("JIRA-ROBOTICS-PLATFORM-4711", "JIRA-ROBOTICS-PLATFORM-4712"):
                fh.write(json.dumps({"occasion_id": occ, "lesson": "a", "injected": True,
                                     "rate": 0.5, "salt": "s"}) + "\n")
        capsys.readouterr()
        main(["doctor", "--dest", str(tmp_path)])
        assert "[OK  ] trace filename collisions" in capsys.readouterr().out
