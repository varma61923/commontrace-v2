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
        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="x", tags=None)
        assert result["traces"] == []
