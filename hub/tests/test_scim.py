"""hub/scim.py: SCIM 2.0 user provisioning, audit 1.2's remaining "no SCIM
auto-provisioning" line.

Two layers, tested separately, in order of how badly getting each wrong
would hurt:

1. **Auth is its own credential class.** A `scim`-scoped key can create
   and deactivate `User` rows and reach nothing else; an ordinary
   read/write/admin key (including a legacy, scopes=None key that
   predates the `scopes` column) must be refused here even though it can
   do everything else in this Hub. This is the property hub/scopes.py's
   `_LEGACY_IMPLIED_SCOPES` split exists for -- getting it wrong would
   silently hand every already-issued full-access production key a new,
   more sensitive capability.
2. **The lifecycle logic itself**: create/get/list/replace/patch/
   deactivate against a real database, tenancy (an org's SCIM key never
   reaches another org's users), and DELETE deactivating rather than
   removing the row -- the same "disabled_at, never a delete" contract
   hub/models.py:User already documents for every other deprovisioning
   path.
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from hub import auth, scim
from hub.abuse import RateLimiter
from hub.db import session_scope
from hub.models import Organization, User

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        organization = Organization(name="scim-org")
        session.add(organization)
        await session.flush()
        return organization.id


@pytest_asyncio.fixture
async def other_org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        organization = Organization(name="scim-other-org")
        session.add(organization)
        await session.flush()
        return organization.id


def _app(session_factory) -> Starlette:
    app = Starlette()
    scim.add_scim_routes(
        app, session_factory,
        auth_rate_limiter=RateLimiter(per_minute=1_000_000, burst=1_000_000),
    )
    return app


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _issue(session_factory, org_id, granted) -> str:
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id, scopes=granted)
    return issued.raw_key


def _auth(raw_key: str) -> dict:
    return {"Authorization": f"Bearer {raw_key}"}


class TestAuthenticationIsItsOwnCredentialClass:
    async def test_no_bearer_header_is_refused(self, session_factory):
        async with _client(_app(session_factory)) as client:
            resp = await client.get("/scim/v2/Users")
        assert resp.status_code == 401

    async def test_a_read_write_admin_key_cannot_reach_scim(self, session_factory, org):
        """The whole point of `scim` being its own scope: a key that can
        do everything else in this Hub still cannot reach this endpoint."""
        raw_key = await _issue(session_factory, org, ["read", "write", "admin"])
        async with _client(_app(session_factory)) as client:
            resp = await client.get("/scim/v2/Users", headers=_auth(raw_key))
        assert resp.status_code == 401

    async def test_a_legacy_key_with_no_scope_list_is_refused(self, session_factory, org):
        """The property hub/scopes.py's `_LEGACY_IMPLIED_SCOPES` split
        exists for: a key issued before the `scopes` column existed (and
        therefore before `scim` existed at all) must not be retroactively
        granted this new, more sensitive capability."""
        raw_key = await _issue(session_factory, org, None)
        async with _client(_app(session_factory)) as client:
            resp = await client.get("/scim/v2/Users", headers=_auth(raw_key))
        assert resp.status_code == 401

    async def test_a_scim_scoped_key_reaches_the_endpoint(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        async with _client(_app(session_factory)) as client:
            resp = await client.get("/scim/v2/Users", headers=_auth(raw_key))
        assert resp.status_code == 200

    async def test_a_revoked_scim_key_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org, scopes=["scim"])
            await auth.revoke_api_key(session, issued.key_id)
        async with _client(_app(session_factory)) as client:
            resp = await client.get("/scim/v2/Users", headers=_auth(issued.raw_key))
        assert resp.status_code == 401


class TestDiscoveryRoutesNeedNoAuth:
    async def test_service_provider_config_is_reachable_unauthenticated(self, session_factory):
        async with _client(_app(session_factory)) as client:
            resp = await client.get("/scim/v2/ServiceProviderConfig")
        assert resp.status_code == 200
        assert resp.json()["patch"]["supported"] is True

    async def test_resource_types_and_schemas_are_reachable_unauthenticated(self, session_factory):
        async with _client(_app(session_factory)) as client:
            r1 = await client.get("/scim/v2/ResourceTypes")
            r2 = await client.get("/scim/v2/Schemas")
        assert r1.status_code == 200
        assert r2.status_code == 200


class TestCoreLifecycle:
    async def test_create_then_get(self, session_factory, org):
        async with session_scope(session_factory) as session:
            user = await scim.create_user(
                session, org, {"userName": "a@example.com", "displayName": "A"}, actor="test",
            )
            user_id = user.id
        async with session_scope(session_factory) as session:
            fetched = await scim.get_user(session, org, user_id)
        assert fetched is not None
        assert fetched.email == "a@example.com"
        assert fetched.role == scim.DEFAULT_PROVISIONED_ROLE
        assert fetched.disabled_at is None

    async def test_created_row_has_no_sso_identity_linked(self, session_factory, org):
        """The module's central safety claim: SCIM creates the row, it
        does not grant a login. Only `hub.manage link-sso` populates
        issuer/external_subject."""
        async with session_scope(session_factory) as session:
            user = await scim.create_user(
                session, org, {"userName": "b@example.com"}, actor="test",
            )
        assert user.issuer == ""
        assert user.external_subject == ""

    async def test_duplicate_username_is_a_conflict(self, session_factory, org):
        async with session_scope(session_factory) as session:
            await scim.create_user(session, org, {"userName": "dup@example.com"}, actor="test")
        async with session_scope(session_factory) as session:
            with pytest.raises(scim.ScimError) as exc:
                await scim.create_user(session, org, {"userName": "dup@example.com"}, actor="test")
        assert exc.value.status == 409

    async def test_missing_username_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(scim.ScimError) as exc:
                await scim.create_user(session, org, {}, actor="test")
        assert exc.value.status == 400

    async def test_list_filters_by_username(self, session_factory, org):
        async with session_scope(session_factory) as session:
            await scim.create_user(session, org, {"userName": "one@example.com"}, actor="test")
            await scim.create_user(session, org, {"userName": "two@example.com"}, actor="test")
        async with session_scope(session_factory) as session:
            total, rows = await scim.list_users(session, org, user_name="two@example.com")
        assert total == 1
        assert rows[0].email == "two@example.com"

    async def test_replace_toggles_active(self, session_factory, org):
        async with session_scope(session_factory) as session:
            user = await scim.create_user(session, org, {"userName": "c@example.com"}, actor="test")
            user_id = user.id
        async with session_scope(session_factory) as session:
            updated = await scim.replace_user(
                session, org, user_id, {"active": False}, actor="test",
            )
        assert updated.disabled_at is not None

    async def test_patch_with_operations_array_deactivates(self, session_factory, org):
        async with session_scope(session_factory) as session:
            user = await scim.create_user(session, org, {"userName": "d@example.com"}, actor="test")
            user_id = user.id
        async with session_scope(session_factory) as session:
            updated = await scim.patch_user(
                session, org, user_id,
                {"Operations": [{"op": "replace", "path": "active", "value": False}]},
                actor="test",
            )
        assert updated.disabled_at is not None

    async def test_patch_accepts_string_boolean_active(self, session_factory, org):
        """At least one real IdP integration sends `"False"` as a string
        rather than a JSON boolean -- see _coerce_active's own docstring."""
        async with session_scope(session_factory) as session:
            user = await scim.create_user(session, org, {"userName": "e@example.com"}, actor="test")
            user_id = user.id
        async with session_scope(session_factory) as session:
            updated = await scim.patch_user(
                session, org, user_id,
                {"Operations": [{"op": "Replace", "path": "active", "value": "False"}]},
                actor="test",
            )
        assert updated.disabled_at is not None

    async def test_patch_with_bare_active_body_is_also_accepted(self, session_factory, org):
        async with session_scope(session_factory) as session:
            user = await scim.create_user(session, org, {"userName": "f@example.com"}, actor="test")
            user_id = user.id
        async with session_scope(session_factory) as session:
            updated = await scim.patch_user(session, org, user_id, {"active": False}, actor="test")
        assert updated.disabled_at is not None

    async def test_patch_leaves_unsupported_attributes_untouched(self, session_factory, org):
        """An operation this module does not model is left alone rather
        than guessed at or rejected -- an IdP that bundles it with a real
        `active` deactivation still gets that deactivation applied."""
        async with session_scope(session_factory) as session:
            user = await scim.create_user(
                session, org, {"userName": "g@example.com", "displayName": "Before"}, actor="test",
            )
            user_id = user.id
        async with session_scope(session_factory) as session:
            updated = await scim.patch_user(
                session, org, user_id,
                {"Operations": [
                    {"op": "replace", "path": "title", "value": "VP of Something"},
                    {"op": "replace", "path": "active", "value": False},
                ]},
                actor="test",
            )
        assert updated.disabled_at is not None
        assert updated.display_name == "Before"

    async def test_delete_deactivates_but_never_removes_the_row(self, session_factory, org):
        async with session_scope(session_factory) as session:
            user = await scim.create_user(session, org, {"userName": "h@example.com"}, actor="test")
            user_id = user.id
        async with session_scope(session_factory) as session:
            deactivated = await scim.deactivate_user(session, org, user_id, actor="test")
        assert deactivated is not None
        assert deactivated.disabled_at is not None
        async with session_scope(session_factory) as session:
            still_there = await session.get(User, user_id)
        assert still_there is not None
        assert still_there.disabled_at is not None

    async def test_reactivating_clears_disabled_at(self, session_factory, org):
        async with session_scope(session_factory) as session:
            user = await scim.create_user(
                session, org, {"userName": "i@example.com", "active": False}, actor="test",
            )
            user_id = user.id
        async with session_scope(session_factory) as session:
            reactivated = await scim.replace_user(session, org, user_id, {"active": True}, actor="test")
        assert reactivated.disabled_at is None

    async def test_tenancy_a_users_own_org_only(self, session_factory, org, other_org):
        async with session_scope(session_factory) as session:
            user = await scim.create_user(session, org, {"userName": "j@example.com"}, actor="test")
            user_id = user.id
        async with session_scope(session_factory) as session:
            assert await scim.get_user(session, other_org, user_id) is None
            assert await scim.replace_user(session, other_org, user_id, {"active": False}, actor="t") is None
            assert await scim.deactivate_user(session, other_org, user_id, actor="t") is None
        async with session_scope(session_factory) as session:
            still_active = await scim.get_user(session, org, user_id)
        assert still_active.disabled_at is None


class TestRoutesEndToEnd:
    async def test_post_then_get_then_patch_then_delete(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        headers = _auth(raw_key)
        async with _client(_app(session_factory)) as client:
            created = await client.post(
                "/scim/v2/Users", headers=headers,
                json={"userName": "k@example.com", "displayName": "K"},
            )
            assert created.status_code == 201
            user_id = created.json()["id"]

            fetched = await client.get(f"/scim/v2/Users/{user_id}", headers=headers)
            assert fetched.status_code == 200
            assert fetched.json()["active"] is True

            patched = await client.patch(
                f"/scim/v2/Users/{user_id}", headers=headers,
                json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
            )
            assert patched.status_code == 200
            assert patched.json()["active"] is False

            deleted = await client.delete(f"/scim/v2/Users/{user_id}", headers=headers)
            assert deleted.status_code == 204

    async def test_filter_by_username(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        headers = _auth(raw_key)
        async with _client(_app(session_factory)) as client:
            await client.post("/scim/v2/Users", headers=headers, json={"userName": "m@example.com"})
            await client.post("/scim/v2/Users", headers=headers, json={"userName": "n@example.com"})
            resp = await client.get(
                "/scim/v2/Users", headers=headers,
                params={"filter": 'userName eq "n@example.com"'},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["totalResults"] == 1
        assert body["Resources"][0]["userName"] == "n@example.com"

    async def test_an_unsupported_filter_shape_is_rejected(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        async with _client(_app(session_factory)) as client:
            resp = await client.get(
                "/scim/v2/Users", headers=_auth(raw_key),
                params={"filter": 'displayName eq "whatever"'},
            )
        assert resp.status_code == 400

    async def test_a_duplicate_username_returns_409(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        headers = _auth(raw_key)
        async with _client(_app(session_factory)) as client:
            first = await client.post("/scim/v2/Users", headers=headers, json={"userName": "dup2@example.com"})
            assert first.status_code == 201
            second = await client.post("/scim/v2/Users", headers=headers, json={"userName": "dup2@example.com"})
        assert second.status_code == 409

    async def test_getting_a_nonexistent_user_is_404(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        async with _client(_app(session_factory)) as client:
            resp = await client.get(
                "/scim/v2/Users/00000000-0000-0000-0000-000000000000", headers=_auth(raw_key),
            )
        assert resp.status_code == 404

    async def test_a_non_uuid_shaped_id_is_404_not_500(self, session_factory, org):
        """id is a UUID column; a malformed id must resolve to the same
        not-found a well-formed-but-nonexistent one gets, not an
        unhandled DBAPIError -- see hub/crud.py:_is_uuid's own docstring."""
        raw_key = await _issue(session_factory, org, ["scim"])
        async with _client(_app(session_factory)) as client:
            resp = await client.get("/scim/v2/Users/does-not-exist", headers=_auth(raw_key))
        assert resp.status_code == 404

    async def test_a_scim_key_cannot_reach_another_orgs_user(self, session_factory, org, other_org):
        org_key = await _issue(session_factory, org, ["scim"])
        other_key = await _issue(session_factory, other_org, ["scim"])
        async with _client(_app(session_factory)) as client:
            created = await client.post(
                "/scim/v2/Users", headers=_auth(org_key), json={"userName": "o@example.com"},
            )
            user_id = created.json()["id"]
            cross_org = await client.get(f"/scim/v2/Users/{user_id}", headers=_auth(other_key))
        assert cross_org.status_code == 404


class TestGroupsAreMembershipOnly:
    """Audit 1.2's other named gap: a real Groups API needs many-to-many
    membership, which hub/rbac.py's single-role-per-user model has no room
    for. These groups exist to hold that membership faithfully, and MUST
    grant nothing -- adding someone to a group changes nothing about what
    they can do."""

    async def test_creating_a_group_grants_no_capability(self, session_factory, org):
        """The central safety claim, checked at the source: hub/rbac.py
        never imports or references ScimGroup at all."""
        import inspect

        from hub import rbac
        source = inspect.getsource(rbac)
        assert "ScimGroup" not in source
        assert "scim_group" not in source.lower()

    async def test_create_then_get(self, session_factory, org):
        async with session_scope(session_factory) as session:
            group = await scim.create_group(
                session, org, {"displayName": "Engineering"}, actor="test",
            )
            group_id = group.id
        async with session_scope(session_factory) as session:
            fetched = await scim.get_group(session, org, group_id)
        assert fetched is not None
        assert fetched.display_name == "Engineering"

    async def test_create_with_members(self, session_factory, org):
        async with session_scope(session_factory) as session:
            user = await scim.create_user(session, org, {"userName": "member@example.com"}, actor="test")
            user_id = user.id
        async with session_scope(session_factory) as session:
            group = await scim.create_group(
                session, org,
                {"displayName": "With Members", "members": [{"value": user_id}]},
                actor="test",
            )
            group_id = group.id
        async with session_scope(session_factory) as session:
            fetched = await scim.get_group(session, org, group_id)
            body = await scim.group_to_scim(session, fetched)
        assert [m["value"] for m in body["members"]] == [user_id]
        assert body["members"][0]["display"] == "member@example.com"

    async def test_a_member_reference_to_another_orgs_user_is_dropped_not_an_error(
        self, session_factory, org, other_org
    ):
        async with session_scope(session_factory) as session:
            outsider = await scim.create_user(
                session, other_org, {"userName": "outsider@example.com"}, actor="test",
            )
            outsider_id = outsider.id
        async with session_scope(session_factory) as session:
            group = await scim.create_group(
                session, org,
                {"displayName": "Cross Org Attempt", "members": [{"value": outsider_id}]},
                actor="test",
            )
            group_id = group.id
        async with session_scope(session_factory) as session:
            fetched = await scim.get_group(session, org, group_id)
            body = await scim.group_to_scim(session, fetched)
        assert body["members"] == []

    async def test_duplicate_display_name_is_a_conflict(self, session_factory, org):
        async with session_scope(session_factory) as session:
            await scim.create_group(session, org, {"displayName": "Dup"}, actor="test")
        async with session_scope(session_factory) as session:
            with pytest.raises(scim.ScimError) as exc:
                await scim.create_group(session, org, {"displayName": "Dup"}, actor="test")
        assert exc.value.status == 409

    async def test_missing_display_name_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(scim.ScimError) as exc:
                await scim.create_group(session, org, {}, actor="test")
        assert exc.value.status == 400

    async def test_list_filters_by_display_name(self, session_factory, org):
        async with session_scope(session_factory) as session:
            await scim.create_group(session, org, {"displayName": "Alpha"}, actor="test")
            await scim.create_group(session, org, {"displayName": "Beta"}, actor="test")
        async with session_scope(session_factory) as session:
            total, rows = await scim.list_groups(session, org, display_name="Beta")
        assert total == 1
        assert rows[0].display_name == "Beta"

    async def test_put_replaces_membership_entirely(self, session_factory, org):
        async with session_scope(session_factory) as session:
            u1 = await scim.create_user(session, org, {"userName": "one@example.com"}, actor="test")
            u2 = await scim.create_user(session, org, {"userName": "two@example.com"}, actor="test")
            u1_id, u2_id = u1.id, u2.id
        async with session_scope(session_factory) as session:
            group = await scim.create_group(
                session, org, {"displayName": "Replaced", "members": [{"value": u1_id}]}, actor="test",
            )
            group_id = group.id
        async with session_scope(session_factory) as session:
            await scim.replace_group(
                session, org, group_id,
                {"displayName": "Replaced", "members": [{"value": u2_id}]},
                actor="test",
            )
        async with session_scope(session_factory) as session:
            fetched = await scim.get_group(session, org, group_id)
            body = await scim.group_to_scim(session, fetched)
        assert [m["value"] for m in body["members"]] == [u2_id]

    async def test_patch_add_member(self, session_factory, org):
        async with session_scope(session_factory) as session:
            user = await scim.create_user(session, org, {"userName": "add@example.com"}, actor="test")
            user_id = user.id
            group = await scim.create_group(session, org, {"displayName": "Add Target"}, actor="test")
            group_id = group.id
        async with session_scope(session_factory) as session:
            await scim.patch_group(
                session, org, group_id,
                {"Operations": [{"op": "add", "path": "members", "value": [{"value": user_id}]}]},
                actor="test",
            )
        async with session_scope(session_factory) as session:
            fetched = await scim.get_group(session, org, group_id)
            body = await scim.group_to_scim(session, fetched)
        assert [m["value"] for m in body["members"]] == [user_id]

    async def test_patch_remove_single_member_via_filtered_path(self, session_factory, org):
        """The `members[value eq "<id>"]` shape real IdPs (Okta among
        them) send for a single-member removal."""
        async with session_scope(session_factory) as session:
            user = await scim.create_user(session, org, {"userName": "rm@example.com"}, actor="test")
            user_id = user.id
            group = await scim.create_group(
                session, org, {"displayName": "Remove Target", "members": [{"value": user_id}]},
                actor="test",
            )
            group_id = group.id
        async with session_scope(session_factory) as session:
            await scim.patch_group(
                session, org, group_id,
                {"Operations": [{"op": "remove", "path": f'members[value eq "{user_id}"]'}]},
                actor="test",
            )
        async with session_scope(session_factory) as session:
            fetched = await scim.get_group(session, org, group_id)
            body = await scim.group_to_scim(session, fetched)
        assert body["members"] == []

    async def test_patch_remove_members_with_no_value_clears_all(self, session_factory, org):
        """RFC 7644's own "remove the whole attribute" shape:
        `{"op": "remove", "path": "members"}` with no `value` at all."""
        async with session_scope(session_factory) as session:
            u1 = await scim.create_user(session, org, {"userName": "clear1@example.com"}, actor="test")
            u2 = await scim.create_user(session, org, {"userName": "clear2@example.com"}, actor="test")
            group = await scim.create_group(
                session, org,
                {"displayName": "Clear All", "members": [{"value": u1.id}, {"value": u2.id}]},
                actor="test",
            )
            group_id = group.id
        async with session_scope(session_factory) as session:
            updated = await scim.patch_group(
                session, org, group_id,
                {"Operations": [{"op": "remove", "path": "members"}]},
                actor="test",
            )
        assert updated is not None
        async with session_scope(session_factory) as session:
            fetched = await scim.get_group(session, org, group_id)
            body = await scim.group_to_scim(session, fetched)
        assert body["members"] == []

    async def test_patch_replaces_display_name(self, session_factory, org):
        async with session_scope(session_factory) as session:
            group = await scim.create_group(session, org, {"displayName": "Old Name"}, actor="test")
            group_id = group.id
        async with session_scope(session_factory) as session:
            updated = await scim.patch_group(
                session, org, group_id,
                {"Operations": [{"op": "replace", "path": "displayName", "value": "New Name"}]},
                actor="test",
            )
        assert updated.display_name == "New Name"

    async def test_patch_leaves_unsupported_operations_untouched(self, session_factory, org):
        async with session_scope(session_factory) as session:
            group = await scim.create_group(session, org, {"displayName": "Untouched"}, actor="test")
            group_id = group.id
        async with session_scope(session_factory) as session:
            updated = await scim.patch_group(
                session, org, group_id,
                {"Operations": [{"op": "replace", "path": "somethingElse", "value": "whatever"}]},
                actor="test",
            )
        assert updated.display_name == "Untouched"

    async def test_delete_actually_removes_the_row(self, session_factory, org):
        """Unlike a User, a group confers no access -- there is no
        deprovisioning history a real delete could falsify, so DELETE
        here really deletes, unlike scim.deactivate_user."""
        from hub.models import ScimGroup
        async with session_scope(session_factory) as session:
            group = await scim.create_group(session, org, {"displayName": "Doomed"}, actor="test")
            group_id = group.id
        async with session_scope(session_factory) as session:
            assert await scim.delete_group(session, org, group_id, actor="test") is True
        async with session_scope(session_factory) as session:
            gone = await session.get(ScimGroup, group_id)
        assert gone is None

    async def test_deleting_a_group_does_not_touch_its_members_user_row(self, session_factory, org):
        async with session_scope(session_factory) as session:
            user = await scim.create_user(session, org, {"userName": "survivor@example.com"}, actor="test")
            user_id = user.id
            group = await scim.create_group(
                session, org, {"displayName": "Temp", "members": [{"value": user_id}]}, actor="test",
            )
            group_id = group.id
        async with session_scope(session_factory) as session:
            await scim.delete_group(session, org, group_id, actor="test")
        async with session_scope(session_factory) as session:
            still_there = await session.get(User, user_id)
        assert still_there is not None
        assert still_there.disabled_at is None

    async def test_tenancy_a_groups_own_org_only(self, session_factory, org, other_org):
        async with session_scope(session_factory) as session:
            group = await scim.create_group(session, org, {"displayName": "Tenant"}, actor="test")
            group_id = group.id
        async with session_scope(session_factory) as session:
            assert await scim.get_group(session, other_org, group_id) is None
            assert await scim.delete_group(session, other_org, group_id, actor="t") is False
        async with session_scope(session_factory) as session:
            still_there = await scim.get_group(session, org, group_id)
        assert still_there is not None


class TestGroupsRoutesEndToEnd:
    async def test_post_then_get_then_patch_then_delete(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        headers = _auth(raw_key)
        async with _client(_app(session_factory)) as client:
            created = await client.post(
                "/scim/v2/Groups", headers=headers, json={"displayName": "Route Group"},
            )
            assert created.status_code == 201
            group_id = created.json()["id"]
            assert created.json()["members"] == []

            fetched = await client.get(f"/scim/v2/Groups/{group_id}", headers=headers)
            assert fetched.status_code == 200
            assert fetched.json()["displayName"] == "Route Group"

            patched = await client.patch(
                f"/scim/v2/Groups/{group_id}", headers=headers,
                json={"Operations": [{"op": "replace", "path": "displayName", "value": "Renamed"}]},
            )
            assert patched.status_code == 200
            assert patched.json()["displayName"] == "Renamed"

            deleted = await client.delete(f"/scim/v2/Groups/{group_id}", headers=headers)
            assert deleted.status_code == 204

            gone = await client.get(f"/scim/v2/Groups/{group_id}", headers=headers)
            assert gone.status_code == 404

    async def test_filter_by_display_name(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        headers = _auth(raw_key)
        async with _client(_app(session_factory)) as client:
            await client.post("/scim/v2/Groups", headers=headers, json={"displayName": "First"})
            await client.post("/scim/v2/Groups", headers=headers, json={"displayName": "Second"})
            resp = await client.get(
                "/scim/v2/Groups", headers=headers,
                params={"filter": 'displayName eq "Second"'},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["totalResults"] == 1
        assert body["Resources"][0]["displayName"] == "Second"

    async def test_an_unsupported_filter_shape_is_rejected(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        async with _client(_app(session_factory)) as client:
            resp = await client.get(
                "/scim/v2/Groups", headers=_auth(raw_key),
                params={"filter": 'userName eq "whatever"'},
            )
        assert resp.status_code == 400

    async def test_a_duplicate_display_name_returns_409(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        headers = _auth(raw_key)
        async with _client(_app(session_factory)) as client:
            first = await client.post("/scim/v2/Groups", headers=headers, json={"displayName": "Dup2"})
            assert first.status_code == 201
            second = await client.post("/scim/v2/Groups", headers=headers, json={"displayName": "Dup2"})
        assert second.status_code == 409

    async def test_getting_a_nonexistent_group_is_404(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        async with _client(_app(session_factory)) as client:
            resp = await client.get(
                "/scim/v2/Groups/00000000-0000-0000-0000-000000000000", headers=_auth(raw_key),
            )
        assert resp.status_code == 404

    async def test_a_non_uuid_shaped_id_is_404_not_500(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        async with _client(_app(session_factory)) as client:
            resp = await client.get("/scim/v2/Groups/does-not-exist", headers=_auth(raw_key))
        assert resp.status_code == 404

    async def test_a_scim_key_cannot_reach_another_orgs_group(self, session_factory, org, other_org):
        org_key = await _issue(session_factory, org, ["scim"])
        other_key = await _issue(session_factory, other_org, ["scim"])
        async with _client(_app(session_factory)) as client:
            created = await client.post(
                "/scim/v2/Groups", headers=_auth(org_key), json={"displayName": "Private"},
            )
            group_id = created.json()["id"]
            cross_org = await client.get(f"/scim/v2/Groups/{group_id}", headers=_auth(other_key))
        assert cross_org.status_code == 404

    async def test_add_then_remove_member_via_patch(self, session_factory, org):
        raw_key = await _issue(session_factory, org, ["scim"])
        headers = _auth(raw_key)
        async with _client(_app(session_factory)) as client:
            user_resp = await client.post(
                "/scim/v2/Users", headers=headers, json={"userName": "route-member@example.com"},
            )
            user_id = user_resp.json()["id"]
            group_resp = await client.post(
                "/scim/v2/Groups", headers=headers, json={"displayName": "Route Members"},
            )
            group_id = group_resp.json()["id"]

            added = await client.patch(
                f"/scim/v2/Groups/{group_id}", headers=headers,
                json={"Operations": [{"op": "add", "path": "members", "value": [{"value": user_id}]}]},
            )
            assert added.status_code == 200
            assert [m["value"] for m in added.json()["members"]] == [user_id]

            removed = await client.patch(
                f"/scim/v2/Groups/{group_id}", headers=headers,
                json={"Operations": [
                    {"op": "remove", "path": f'members[value eq "{user_id}"]'},
                ]},
            )
            assert removed.status_code == 200
            assert removed.json()["members"] == []
