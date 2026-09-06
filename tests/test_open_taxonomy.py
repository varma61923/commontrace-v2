"""The taxonomy is open: any field is a first-class agent_type.

protocol/PROTOCOL.md#7-taxonomy-open-not-closed defines the taxonomy as open,
trace.schema.json/lesson.schema.json declare `agent_type` as a plain string
with no enum, and the Hub stores it as free text. The CLI was the one surface
that disagreed, in two places that compounded:

1. `choices=paths.AGENT_TYPES` on init/lesson new/import made a robotics or
   legal fleet impossible to declare at all.
2. `paths.store_agent_type` then validated by MEMBERSHIP in that same list, so
   a store whose INDEX.md said `robotics` read back as `code` -- silently --
   and every trace it captured was stamped with the wrong fleet.

Together those made "works for any agent type" untrue in the one place a
customer would find out slowly: their data.
"""
import os

import pytest

from commontrace import distill, paths, templates
from commontrace.cli import main
from commontrace.commands import _validators


class TestAnyFieldCanBeDeclared:
    @pytest.mark.parametrize("agent_type", ["robotics", "legal", "biotech", "field-ops", "tier2_support"])
    def test_init_accepts_a_field_with_no_starter_vocabulary(self, tmp_path, agent_type):
        assert main(["init", "--agent-type", agent_type, "--dest", str(tmp_path)]) == 0
        assert paths.store_agent_type(str(tmp_path)) == agent_type

    def test_the_declared_type_survives_the_round_trip_into_a_trace(self, tmp_path):
        """The regression that mattered: a robotics store stamping `code`."""
        dest = str(tmp_path)
        assert main(["init", "--agent-type", "robotics", "--dest", dest]) == 0
        assert main([
            "capture", "--title", "Arm drift after long runtime",
            "--context", "End-effector drifts a few mm after hours of operation.",
            "--solution", "Scheduled fiducial re-zero every 90 minutes.",
            "--occasion-id", "RB-1", "--dest", dest,
        ]) == 0

        traces = paths.traces_dir(dest)
        captured = [f for f in os.listdir(traces) if f.endswith(".md") and f != "README.md"]
        assert len(captured) == 1
        body = (tmp_path / "memory" / "traces" / captured[0]).read_text(encoding="utf-8")
        assert "agent_type: robotics" in body
        assert "agent_type: code" not in body

    def test_suggested_types_are_examples_not_a_gate(self):
        assert "robotics" not in paths.SUGGESTED_AGENT_TYPES
        assert paths.AGENT_TYPE_RE.match("robotics")
        # The deprecated alias still resolves for one release.
        assert paths.AGENT_TYPES == paths.SUGGESTED_AGENT_TYPES


class TestShapeIsStillValidated:
    """Open does not mean anything goes: the value is written unquoted into
    memory/INDEX.md's first line and into the Hub's String(64) column."""

    @pytest.mark.parametrize("bad", ["Robotics Fleet!", "../escape", "a b", "-leading", "", "x" * 65])
    def test_rejected_at_the_cli_boundary(self, bad):
        with pytest.raises(Exception):
            _validators.agent_type(bad)

    def test_normalizes_case_and_whitespace(self):
        assert _validators.agent_type("  Robotics  ") == "robotics"

    def test_an_unusable_declared_type_falls_back_loudly(self, tmp_path, capsys):
        """Silence is what made the original bug invisible for so long."""
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "INDEX.md").write_text(
            "# Memory Index — agent_type: not a valid slug!\n", encoding="utf-8"
        )
        assert paths.store_agent_type(str(tmp_path)) == "code"
        assert "not a valid slug" in capsys.readouterr().err


class TestEpisodesFollowTheProfileNotTheAgentType:
    """`memory/episodes/` is written by the code-review profile's pipeline
    (SKILL.md), not by the `code` agent type. Keying the store layout on the
    agent type made `code` the only first-class fleet."""

    def test_default_init_still_scaffolds_episodes(self, tmp_path):
        assert main(["init", "--dest", str(tmp_path)]) == 0
        assert os.path.isdir(paths.episodes_dir(str(tmp_path)))

    def test_a_non_code_fleet_gets_no_episodes_by_default(self, tmp_path):
        assert main(["init", "--agent-type", "robotics", "--dest", str(tmp_path)]) == 0
        assert not os.path.isdir(paths.episodes_dir(str(tmp_path)))

    def test_any_fleet_may_opt_into_the_episode_profile(self, tmp_path):
        assert main([
            "init", "--agent-type", "legal", "--profile", "code-review", "--dest", str(tmp_path),
        ]) == 0
        assert os.path.isdir(paths.episodes_dir(str(tmp_path)))

    def test_a_code_fleet_may_opt_out(self, tmp_path):
        assert main(["init", "--agent-type", "code", "--profile", "", "--dest", str(tmp_path)]) == 0
        assert not os.path.isdir(paths.episodes_dir(str(tmp_path)))

    def test_the_index_points_at_wherever_capture_actually_lands(self, tmp_path):
        assert main(["init", "--agent-type", "robotics", "--dest", str(tmp_path)]) == 0
        index = (tmp_path / "memory" / "INDEX.md").read_text(encoding="utf-8")
        assert "#### Traces" in index
        assert "#### Episodes" not in index


class TestDistilledDomainsAreNotAllOther:
    """`propose_domain` used to label every candidate from a fleet with no
    STARTER_DOMAINS entry `other`, collapsing that field's whole taxonomy into
    one bucket and making `commontrace taxonomy`'s coverage report useless."""

    def _cluster(self, tags, agent_type="robotics"):
        traces = [
            distill.TraceCandidate(
                id=f"t{i}", path=f"/tmp/t{i}.md",
                title="Sensor jitter after firmware update",
                context_text="Fused pose estimate is noisy since flashing new firmware.",
                solution_text="Re-synced the timestamp offset against a hardware trigger.",
                tags=list(tags), agent_type=agent_type,
            )
            for i in range(2)
        ]
        return distill.Cluster(traces=traces, shared_terms=["timestamp", "firmware"])

    def test_uses_the_fields_own_vocabulary(self):
        cluster = self._cluster(["sensor-fusion", "firmware"])
        assert distill.propose_domain(cluster, "robotics") == "sensor-fusion"

    def test_falls_back_to_a_shared_term_before_other(self):
        cluster = self._cluster([])
        assert distill.propose_domain(cluster, "robotics") == "timestamp"

    def test_a_listed_fleets_starter_domain_still_wins(self):
        cluster = self._cluster(["compliance", "misc"], agent_type="hr")
        assert distill.propose_domain(cluster, "hr") == "compliance"


class TestDoctorReportsWhatCommandsWillActuallyUse:
    def test_reports_the_declared_type_when_it_is_honoured(self, tmp_path, capsys):
        assert main(["init", "--agent-type", "robotics", "--dest", str(tmp_path)]) == 0
        capsys.readouterr()
        main(["doctor", "--dest", str(tmp_path)])
        out = capsys.readouterr().out
        assert "store agent_type" in out
        assert "robotics" in out

    def test_flags_a_store_whose_declared_type_is_being_ignored(self, tmp_path, capsys):
        assert main(["init", "--agent-type", "robotics", "--dest", str(tmp_path)]) == 0
        index = tmp_path / "memory" / "INDEX.md"
        rest = index.read_text(encoding="utf-8").split("\n", 1)[1]
        index.write_text("# Memory Index — agent_type: NOT A SLUG\n" + rest, encoding="utf-8")
        capsys.readouterr()
        main(["doctor", "--dest", str(tmp_path)])
        out = capsys.readouterr().out
        assert "[WARN] store agent_type" in out


def test_index_heading_helper_is_keyed_on_episodes_not_agent_type():
    assert "#### Episodes" in templates.index_md("legal", has_episodes=True)
    assert "#### Traces" in templates.index_md("code", has_episodes=False)
