"""A caller-supplied `tags` that is the wrong SHAPE -- a bare string
instead of a list, or a list containing a non-string element -- must get a
clean 400, not a crash.

contribute_trace/amend_trace/submit_kb_entry are already protected against
both shapes by trace.schema.json's `"tags": {"type": "array", "items":
{"type": "string"}}` constraint, enforced by validate_trace before
anything downstream ever sees the value. search_traces has no schema
validation ahead of it at all -- it is a read path, not a write path -- so
it was the one place in this Hub where neither shape was actually checked:

- `tags="abc"` (a string, not a list): `for tag in tags or []` iterates
  the string's own characters (each individually a valid one-character
  str), so nothing in that loop objects, and the ORIGINAL unmodified
  string then reaches the query's array-typed bind parameter and crashes
  with an uncaught `ProgrammingError` ("operator does not exist:
  character varying[] && character varying").
- `tags=[123]` (a list containing a non-string element): reached
  `reject_unstorable_text`'s `"\\x00" in value` with `value=123` and
  crashed with an uncaught `TypeError: argument of type 'int' is not
  iterable` -- not a ValueError, so it fell through the same
  hub/server.py:_error_response gap every other fix in this session
  exists to close.

Both reproduced against a live Postgres before fixing. The fix has two
parts: reject_unstorable_text itself now checks `isinstance(value, str)`
first (protecting every one of its ~15 call sites, not just this one, the
same way the earlier NUL-byte/surrogate fix was applied as one change to
the shared function rather than patched at each site individually), and
search_traces separately checks that `tags` itself is a list before
iterating it at all -- a check reject_unstorable_text alone cannot provide,
since iterating a string's own characters never raises.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from hub import crud
from hub.abuse import reject_unstorable_text
from hub.db import session_scope
from hub.models import Organization

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="tags-type-safety-org")
        session.add(o)
        await session.flush()
        return o.id


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestRejectUnstorableTextRejectsNonStrings:
    @pytest.mark.parametrize("bad", [123, None, 1.5, ["nested"], {"k": "v"}, True])
    def test_a_non_string_value_is_rejected_naming_the_field_and_type(self, bad):
        with pytest.raises(ValueError, match="field"):
            reject_unstorable_text(bad, "field")

    def test_the_message_names_the_actual_type(self):
        with pytest.raises(ValueError, match="int"):
            reject_unstorable_text(123, "field")


class TestSearchTracesTagsShape:
    @pytest.mark.parametrize(
        "bad_tags", ["abc", 123, {"a": 1}, True],
        ids=["string", "int", "dict", "bool"],
    )
    async def test_a_non_list_tags_is_rejected_not_crashed(self, session_factory, org, bad_tags):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="tags"):
                await crud.search_traces(session, org, query="x", tags=bad_tags)

    @pytest.mark.parametrize("bad_element", [123, None, 1.5, ["nested"]], ids=["int", "none", "float", "list"])
    async def test_a_non_string_element_in_an_otherwise_valid_list_is_rejected(
        self, session_factory, org, bad_element
    ):
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="tag"):
                await crud.search_traces(session, org, query="x", tags=["fine", bad_element])

    async def test_a_genuine_list_of_strings_still_works(self, session_factory, org, config):
        """False-positive guard, matching this session's established
        practice: the fix must not reject the ordinary, correct case."""
        from hub.abuse import make_rate_limiter

        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s",
                tags=["real-tag"], agent_type="code", actor="test",
            )
        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, tags=["real-tag"])
        assert result["traces"]

    async def test_none_tags_still_means_no_filter(self, session_factory, org):
        """The default -- omitting tags entirely -- must remain unaffected
        by the new isinstance check."""
        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="x", tags=None)
        assert result["traces"] == []
