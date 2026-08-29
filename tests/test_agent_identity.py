"""`capture --agent-id` and the maxLength validation it needed.

agent_type is a CATEGORY ("support"); agent_id is the IDENTITY. Only the
second can answer "how many agents does this fleet run", which is the
number a Hub plan's agent limit is enforced against (hub/plans.py) and the
variable STRATEGY.md §12.6 concludes the business should be run on.
"""
from __future__ import annotations

import os

import pytest

from commontrace import frontmatter, trace_io, validate
from commontrace.cli import main


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    return tmp_path


def _only_trace(store):
    tdir = store / "memory" / "traces"
    paths = [p for p in tdir.glob("*.md") if p.name != "README.md"]
    assert len(paths) == 1
    return trace_io.read(str(paths[0]))[0]


def _capture(store, *extra):
    return main([
        "capture", "--title", "Refund delayed", "--context", "customer asked twice",
        "--solution", "state the window up front", "--agent-type", "support",
        "--dest", str(store), *extra,
    ])


class TestCaptureAgentId:
    def test_agent_id_is_recorded_when_given(self, store):
        assert _capture(store, "--agent-id", "support-worker-7") == 0
        assert _only_trace(store)["agent_id"] == "support-worker-7"

    def test_agent_id_defaults_to_empty_and_stays_valid(self, store):
        """Backward compatible: omitting it must not fail validation, because
        every trace captured before agent identity existed had no such field."""
        assert _capture(store) == 0
        instance = _only_trace(store)
        assert instance["agent_id"] == ""
        assert validate.validate(instance, validate.load_schema("trace.schema.json")) == []

    def test_two_agents_of_the_same_type_are_distinguishable(self, store):
        """The whole point: agent_type alone collapses a fleet into one value."""
        _capture(store, "--agent-id", "worker-a")
        main([
            "capture", "--title", "Another", "--context", "ctx", "--solution", "fix",
            "--agent-type", "support", "--agent-id", "worker-b", "--dest", str(store),
        ])
        tdir = store / "memory" / "traces"
        ids = set()
        types = set()
        for p in tdir.glob("*.md"):
            if p.name == "README.md":
                continue
            fm, _ = frontmatter.read(str(p))
            ids.add(fm.get("agent_id"))
            types.add(fm.get("agent_type"))
        assert ids == {"worker-a", "worker-b"}
        assert types == {"support"}  # indistinguishable on type alone

    def test_an_oversized_agent_id_is_refused_at_capture_not_at_sync(self, store, capsys):
        """The Hub's column is String(128) and it rejects longer values
        server-side. Catching it locally means the failure lands on the
        person who can fix it, at the moment they cause it."""
        rc = _capture(store, "--agent-id", "x" * 129)
        assert rc != 0
        assert "maxLength" in capsys.readouterr().err
        tdir = store / "memory" / "traces"
        assert [p for p in tdir.glob("*.md") if p.name != "README.md"] == []

    def test_an_agent_id_at_exactly_the_limit_is_accepted(self, store):
        assert _capture(store, "--agent-id", "x" * 128) == 0


class TestValidatorMaxLength:
    """maxLength had to be implemented before the schema could use it:
    commontrace/validate.py enforces a deliberate subset and raises rather
    than silently ignoring an unknown keyword, so an unimplemented
    constraint cannot ship as a no-op."""

    def test_over_the_limit_is_an_error(self):
        errors = validate.validate({"s": "abcd"}, {"properties": {"s": {"type": "string", "maxLength": 3}}})
        assert len(errors) == 1
        assert "maxLength=3" in errors[0]

    def test_at_and_under_the_limit_pass(self):
        schema = {"properties": {"s": {"type": "string", "maxLength": 3}}}
        assert validate.validate({"s": "abc"}, schema) == []
        assert validate.validate({"s": "ab"}, schema) == []

    def test_it_composes_with_minlength(self):
        schema = {"properties": {"s": {"type": "string", "minLength": 2, "maxLength": 4}}}
        assert validate.validate({"s": "abc"}, schema) == []
        assert len(validate.validate({"s": "a"}, schema)) == 1
        assert len(validate.validate({"s": "abcde"}, schema)) == 1

    def test_the_keyword_is_registered_as_supported(self):
        """If it were not, assert_supported_schema would reject the shipped
        schema -- which is exactly how this gap was caught."""
        assert "maxLength" in validate._SUPPORTED_KEYWORDS
        validate.assert_supported_schema(validate.load_schema("trace.schema.json"))


class TestHubClientForwardsAgentId:
    def test_sync_payload_includes_agent_id(self):
        """A fleet whose traces carry agent_id must not have it dropped on the
        way to the Hub -- the Hub is where the agent limit is enforced, so a
        silently-dropped field would collapse the whole fleet into the single
        'unattributed' agent."""
        src = open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "commontrace", "hub_client.py"),
            encoding="utf-8",
        ).read()
        assert '"agent_id": fm.get("agent_id") or ""' in src
