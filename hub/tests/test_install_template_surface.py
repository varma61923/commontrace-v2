"""The install template must advertise the Hub's real tool surface.

`commontrace install --target generic-mcp` writes an MCP config whose
comment names the tools the Hub exposes. That list had drifted to the
original six while the Hub kept growing -- so a customer wiring up
their agent platform was told the Hub could do a third of what it does,
including none of the measurement tools.

Drift like this is silent by construction: the generated file is still
valid JSON, still connects, and still works for the six tools it names.
Nothing fails. The customer simply never learns the rest exist, which for
the holdout tools means the experiment STRATEGY.md §13.2 calls the
cheapest falsifier available never gets run.

Lives in hub/tests/ rather than tests/ because it imports hub.smoke, and
the client package must keep installing with PyYAML alone.
"""
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
        """The comment embeds the tool list; a previous bug let its quotes
        leak in unescaped and break parsing."""
        doc = json.loads(install_cmd._hub_mcp_example())
        for tool in smoke.EXPECTED_TOOLS:
            assert tool in doc["_comment"], tool


class TestAgentsAreToldHowToMeasure:
    """The generated pointer skill is the only thing an agent installed via
    `commontrace install` ever reads. An agent never told about the holdout
    will never call it, and the experiment never runs -- so the instrument
    existing is not sufficient, it has to be described where agents look.
    """

    def test_the_pointer_skill_teaches_the_holdout_loop(self):
        skill = install_cmd._GENERIC_POINTER_SKILL
        assert "occasion_id" in skill
        assert "record_occasion_outcome" in skill
        assert "holdout" in skill.lower()

    def test_it_states_the_rule_that_fails_silently(self):
        """Using a withheld trace does not raise. If the instructions do not
        say so, an agent will do it and nobody will find out."""
        skill = install_cmd._GENERIC_POINTER_SKILL
        assert "Do not use any trace listed under" in skill
        assert "biases the measured effect toward zero" in skill

    def test_it_covers_both_tiers(self):
        skill = install_cmd._GENERIC_POINTER_SKILL
        assert "search_traces" in skill
        assert "commontrace query --experiment" in skill


class TestTheReferenceProfileAlsoTeachesIt:
    """`install --target claude-code` copies the repo's own SKILL.md when
    one is found, and only falls back to the pointer skill when it is not.

    Anyone installing from a checkout -- the common case -- therefore gets
    SKILL.md, so teaching the holdout only in the fallback fixes the path
    fewer people take. That is exactly what happened: the fallback was
    updated first and this file's other tests passed while the primary
    path still taught nothing about measurement.
    """

    @staticmethod
    def _skill() -> str:
        """Whitespace-normalized, so an assertion about what the document
        SAYS does not fail because a sentence happened to wrap. Prose gets
        reflowed; the claim is what has to stay."""
        import pathlib
        import re

        text = (pathlib.Path(__file__).resolve().parents[2] / "SKILL.md").read_text(
            encoding="utf-8"
        )
        return re.sub(r"\s+", " ", text)

    def test_the_retrieval_phase_honours_a_running_holdout(self):
        """Retrieval is the only point that knows which lessons were
        ELIGIBLE, and eligibility is what makes the comparison causal
        rather than confounded -- so the instruction belongs there and
        nowhere else."""
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
        """Without a slot in the required output block, an agent honouring
        the holdout has no way to say so, and a reviewer cannot tell a
        withheld lesson from one that simply did not match."""
        assert "### Withheld by the holdout" in self._skill()


class TestInstallCmdStaysImportableWithoutTheClientDependencies:
    """This file imports `commontrace.commands.install_cmd`, and the job that
    runs it installs `hub/requirements.txt` only -- **no PyYAML**, because the
    Hub server does not need it.

    That constraint was invisible until it broke. `install_cmd` grew a
    module-level `from commontrace import mcp_server` to read a tuple of tool
    NAMES, and `mcp_server` pulls in the retrieval stack
    (`evidence_io` -> `frontmatter` -> `yaml`). Every hub-tests job failed at
    collection, on all three Python versions at once, with a
    `ModuleNotFoundError` about a package nothing in hub/ uses -- and the full
    local suite passed the whole time, because a development machine has
    PyYAML.

    Asserted in a subprocess with `yaml` blocked, which is the only way to
    reproduce it from an environment that has the package installed.
    """

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
        """`mcp_tools` exists so the names can be read without dragging in the
        server that serves them."""
        result = self._import_with_yaml_blocked("commontrace.mcp_tools")
        assert result.returncode == 0, result.stderr
