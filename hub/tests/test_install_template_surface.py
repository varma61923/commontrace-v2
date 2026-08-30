"""The install template must advertise the Hub's real tool surface.

`commontrace install --target generic-mcp` writes an MCP config whose
comment names the tools the Hub exposes. That list had drifted to the
original six while the Hub grew to eighteen -- so a customer wiring up
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
