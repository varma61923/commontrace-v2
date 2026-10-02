from __future__ import annotations

import json
import pathlib
import subprocess
import sys

from commontrace.commands import install_cmd
from hub import smoke


class TestAdvertisedSurfaceMatchesReality:
    def test_the_template_lists_exactly_the_hub_tools(self):
        assert sorted(install_cmd._HUB_TOOLS) == sorted(smoke.EXPECTED_TOOLS), (
            "commontrace/commands/install_cmd.py:_HUB_TOOLS has drifted from "
            "hub/smoke.py:EXPECTED_TOOLS. The generated MCP config tells customers "
            "what the Hub can do; a stale list is silent -- the file still works for "
            "the tools it names, and the rest are simply never discovered."
        )

    def test_no_duplicates_in_the_advertised_list(self):
        assert len(install_cmd._HUB_TOOLS) == len(set(install_cmd._HUB_TOOLS))

    def test_the_generated_config_is_valid_json_and_names_every_tool(self):
        doc = json.loads(install_cmd._hub_mcp_example())
        for tool in smoke.EXPECTED_TOOLS:
            assert tool in doc["_comment"], tool


class TestAgentsAreToldHowToMeasure:
    def test_the_pointer_skill_teaches_the_holdout_loop(self):
        skill = install_cmd._GENERIC_POINTER_SKILL
        assert "occasion_id" in skill
        assert "record_occasion_outcome" in skill
        assert "holdout" in skill.lower()

    def test_it_states_the_rule_that_fails_silently(self):
        skill = install_cmd._GENERIC_POINTER_SKILL
        assert "Do not use any trace listed under" in skill
        assert "biases the measured effect toward zero" in skill

    def test_it_covers_both_tiers(self):
        skill = install_cmd._GENERIC_POINTER_SKILL
        assert "search_traces" in skill
        assert "commontrace query --experiment" in skill


class TestTheReferenceProfileAlsoTeachesIt:
    @staticmethod
    def _skill() -> str:
        import pathlib
        import re

        text = (pathlib.Path(__file__).resolve().parents[2] / "SKILL.md").read_text(
            encoding="utf-8"
        )
        return re.sub(r"\s+", " ", text)

    def test_the_retrieval_phase_honours_a_running_holdout(self):
        skill = self._skill()
        assert "randomized holdout is running" in skill
        assert "occasion_id" in skill
        assert "record_occasion_outcome" in skill
        assert "commontrace query --experiment" in skill

    def test_it_states_the_rule_the_tooling_cannot_enforce(self):
        skill = self._skill()
        assert "does not raise an error" in skill
        assert "biasing the measured effect toward zero" in skill

    def test_the_output_format_has_somewhere_to_report_withheld_lessons(self):
        assert "### Withheld by the holdout" in self._skill()


class TestInstallCmdStaysImportableWithoutTheClientDependencies:
    @staticmethod
    def _import_with_yaml_blocked(module: str) -> subprocess.CompletedProcess:
        blocker = (
            "import sys\n"
            "class B:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'yaml' or name.startswith('yaml.'):\n"
            "            raise ModuleNotFoundError(\"No module named 'yaml'\")\n"
            "        return None\n"
            "sys.meta_path.insert(0, B())\n"
            f"import {module}\n"
            "print('ok')\n"
        )
        return subprocess.run(
            [sys.executable, "-c", blocker],
            capture_output=True, text=True,
            cwd=str(pathlib.Path(__file__).resolve().parents[2]), check=False,
        )

    def test_install_cmd_imports_without_pyyaml(self):
        result = self._import_with_yaml_blocked("commontrace.commands.install_cmd")
        assert result.returncode == 0, (
            "hub/tests/ imports install_cmd, and this job installs no PyYAML. "
            "Something in install_cmd's import chain now needs it:\n" + result.stderr
        )

    def test_the_tool_names_come_from_a_dependency_free_module(self):
        result = self._import_with_yaml_blocked("commontrace.mcp_tools")
        assert result.returncode == 0, result.stderr
