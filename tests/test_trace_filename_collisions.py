"""Two distinct occasions must never share a trace file.

`capture` named a trace `<date>_<title-slug>_<id-suffix>.md`, where the suffix
was the FIRST 16 CHARACTERS of the sanitized occasion id. Two occasions on the
same day, with the same title, whose ids differ only after character 16
therefore computed the same path -- and nothing checked, because
`_find_trace_by_occasion` correctly declines to match them (it compares the
full id inside the file). `frontmatter.write` uses os.replace, which is atomic
and silently clobbering. The second capture destroyed the first.

The inputs that trigger it are the ones PILOT.md tells operators to use: "a
ticket number, a run id, a job id". Real ones are prefixed --
JIRA-ROBOTICS-PLATFORM-4711 and ...-4712 share 25 characters. Reproduced
before the fix: five captures under such ids left ONE file on disk, an 80%
silent loss rate.

No test covered this. The nearest one used a single occasion id.
"""
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
        """A uuid4's leading characters and a short ticket id were already
        safe and readable; only the truncating case needed to change."""
        assert capture_cmd._id_suffix("TICKET-42") == "TICKET-42"

    def test_it_stays_deterministic(self):
        """`_find_trace_by_occasion`'s fast-path glob recomputes this to
        locate an existing trace, so a random component would break
        re-capture."""
        assert capture_cmd._id_suffix("JIRA-ROBOTICS-PLATFORM-4711") == \
            capture_cmd._id_suffix("JIRA-ROBOTICS-PLATFORM-4711")

    def test_it_stays_filesystem_safe(self):
        suffix = capture_cmd._id_suffix("../../etc/passwd and a space/../..")
        assert "/" not in suffix and " " not in suffix and ".." not in suffix

    def test_an_id_that_sanitizes_to_nothing_still_yields_a_name(self):
        assert capture_cmd._id_suffix("///") == "trace"


class TestNoCaptureIsLost:
    def test_five_ticket_ids_sharing_a_prefix_produce_five_traces(self, tmp_path):
        """The reproduction, verbatim. Before the fix this left one file."""
        assert main(["init", "--dest", str(tmp_path)]) == 0
        ids = [f"JIRA-ROBOTICS-PLATFORM-471{i}" for i in range(5)]
        for i, occ in enumerate(ids):
            assert _capture(tmp_path, "Sensor jitter after firmware update", occ,
                            context=f"ctx {i}", solution=f"sol {i}") == 0

        assert _trace_ids(tmp_path) == set(ids)

    def test_each_trace_keeps_its_own_content(self, tmp_path):
        """Not just the right number of files -- the right contents. A
        collision that produced five files with one trace's body repeated
        would pass a headcount."""
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
        """The fix must not break the case the suffix scheme exists for:
        re-capturing under the same id attaches an outcome to the SAME trace."""
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
        """The point of all of this. A lost trace is a lost outcome, and a
        lost outcome is an occasion that silently leaves the experiment."""
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
        """Detection reads the append-only holdout log, because the traces
        are the thing that was destroyed."""
        assert main(["init", "--dest", str(tmp_path)]) == 0
        log = os.path.join(paths.memory_dir(str(tmp_path)), "holdout_log.jsonl")
        with open(log, "w", encoding="utf-8") as fh:
            for occ in ("JIRA-ROBOTICS-PLATFORM-4711", "JIRA-ROBOTICS-PLATFORM-4712"):
                fh.write(json.dumps({"occasion_id": occ, "lesson": "a", "injected": True,
                                     "rate": 0.5, "salt": "s"}) + "\n")
        # Only the second survived the clobber.
        _capture(tmp_path, "Sensor jitter", "JIRA-ROBOTICS-PLATFORM-4712")

        capsys.readouterr()
        main(["doctor", "--dest", str(tmp_path)])
        assert "[WARN] trace filename collisions" in capsys.readouterr().out

    def test_a_prefix_pair_with_neither_captured_is_not_flagged(self, tmp_path, capsys):
        """An occasion with no trace yet is the normal mid-pilot state, not
        evidence of loss. Flagging it would train people to ignore this."""
        assert main(["init", "--dest", str(tmp_path)]) == 0
        log = os.path.join(paths.memory_dir(str(tmp_path)), "holdout_log.jsonl")
        with open(log, "w", encoding="utf-8") as fh:
            for occ in ("JIRA-ROBOTICS-PLATFORM-4711", "JIRA-ROBOTICS-PLATFORM-4712"):
                fh.write(json.dumps({"occasion_id": occ, "lesson": "a", "injected": True,
                                     "rate": 0.5, "salt": "s"}) + "\n")
        capsys.readouterr()
        main(["doctor", "--dest", str(tmp_path)])
        assert "[OK  ] trace filename collisions" in capsys.readouterr().out
