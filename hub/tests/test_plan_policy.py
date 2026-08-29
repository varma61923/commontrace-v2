"""Plan policy invariants -- pure, synchronous, no database.

Kept out of hub/tests/test_agents.py because that module carries a
module-level asyncio mark, and a synchronous test inheriting it warns on
every run. The separation is mechanical, not conceptual: these guard the
same agent entitlement the tests there exercise against a real database.
"""
from __future__ import annotations

from hub import plans


class TestPlanInvariant:
    def test_every_plan_sets_max_agents_explicitly(self):
        """Plan.max_agents carries a permissive default so ad-hoc Plans in
        tests don't accidentally enforce an agent cap they were not written
        to test. That default must never reach a real customer, so every
        shipped plan is required to set it.

        Checked by parsing the AST rather than the source text: a
        string-matching version passes or fails on formatting, which is
        exactly the wrong sensitivity for an invariant guarding a
        fail-open default.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(plans))
        seen: set[str] = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Dict) and node.keys):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    continue
                if not (isinstance(value, ast.Call) and getattr(value.func, "id", "") == "Plan"):
                    continue
                kwargs = {kw.arg for kw in value.keywords}
                assert "max_agents" in kwargs, (
                    f"plan '{key.value}' does not set max_agents explicitly, so it "
                    "silently inherits the permissive default"
                )
                seen.add(key.value)
        assert seen == set(plans.PLANS), (
            f"AST scan found {sorted(seen)} but PLANS defines {sorted(plans.PLANS)} -- "
            "the invariant is not actually covering every plan"
        )

    def test_the_billable_tiers_match_the_shape_being_sold(self):
        assert plans.get("free").max_agents == 5
        assert plans.get("team").max_agents == 25
        assert plans.get("scale").max_agents == plans.UNLIMITED

    def test_an_unknown_plan_still_fails_closed_on_agents(self):
        assert plans.get("nonsense").max_agents == plans.get("free").max_agents
