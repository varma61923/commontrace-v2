"""hub/disclosure.py -- audit 2.3/4.4's "unknown data residency"/"no
canonical product identity" gaps get a PLACE to be answered, once an
operator has real answers. No claim is asserted on this codebase's own
behalf: an unconfigured deployment says exactly that.
"""
from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette

from hub.config import HubConfig
from hub.disclosure import add_disclosure_route

pytestmark = pytest.mark.asyncio


def _app(**config_kwargs) -> Starlette:
    app = Starlette()
    config_kwargs.setdefault("database_url", "postgresql+asyncpg://unused/unused")
    add_disclosure_route(app, HubConfig(**config_kwargs))
    return app


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestUnconfiguredSaysSoExplicitly:
    async def test_every_field_says_not_disclosed(self):
        async with _client(_app()) as client:
            resp = await client.get("/disclosure")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_region"] == "not disclosed by this deployment's operator"
        assert body["operator_legal_name"] == "not disclosed by this deployment's operator"
        assert body["operator_support_contact"] == "not disclosed by this deployment's operator"

    async def test_nothing_is_silently_omitted(self):
        """A missing key and an explicit 'not disclosed' are different
        claims -- a reviewer must never have to guess which one this is."""
        async with _client(_app()) as client:
            resp = await client.get("/disclosure")
        body = resp.json()
        assert "data_region" in body
        assert "operator_legal_name" in body
        assert "operator_support_contact" in body

    async def test_asserts_no_certification(self):
        async with _client(_app()) as client:
            resp = await client.get("/disclosure")
        note = resp.json()["note"].lower()
        assert "not independently verified" in note
        assert "no certification" in note or "asserts no" in note


class TestConfiguredValuesAreReturnedVerbatim:
    async def test_a_configured_region_is_reported(self):
        async with _client(_app(data_region="us-east-1")) as client:
            resp = await client.get("/disclosure")
        assert resp.json()["data_region"] == "us-east-1"

    async def test_a_configured_legal_name_is_reported(self):
        async with _client(_app(operator_legal_name="Northwind Trading Co.")) as client:
            resp = await client.get("/disclosure")
        assert resp.json()["operator_legal_name"] == "Northwind Trading Co."

    async def test_a_configured_support_contact_is_reported(self):
        async with _client(_app(operator_support_contact="support@example.com")) as client:
            resp = await client.get("/disclosure")
        assert resp.json()["operator_support_contact"] == "support@example.com"

    async def test_whitespace_only_is_treated_as_unconfigured(self):
        async with _client(_app(data_region="   ")) as client:
            resp = await client.get("/disclosure")
        assert resp.json()["data_region"] == "not disclosed by this deployment's operator"


class TestUnauthenticated:
    async def test_no_authorization_header_needed(self):
        async with _client(_app()) as client:
            resp = await client.get("/disclosure")
        assert resp.status_code == 200

    async def test_only_get_is_wired(self):
        async with _client(_app()) as client:
            resp = await client.post("/disclosure")
        assert resp.status_code == 405


class TestConfigFromEnv:
    async def test_env_vars_populate_the_config_fields(self, monkeypatch):
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://unused/unused")
        monkeypatch.setenv("HUB_DATA_REGION", "eu-west-1")
        monkeypatch.setenv("HUB_OPERATOR_LEGAL_NAME", "Acme GmbH")
        monkeypatch.setenv("HUB_OPERATOR_SUPPORT_CONTACT", "help@acme.example")
        config = HubConfig.from_env()
        assert config.data_region == "eu-west-1"
        assert config.operator_legal_name == "Acme GmbH"
        assert config.operator_support_contact == "help@acme.example"

    async def test_unset_env_vars_default_to_empty(self, monkeypatch):
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://unused/unused")
        monkeypatch.delenv("HUB_DATA_REGION", raising=False)
        monkeypatch.delenv("HUB_OPERATOR_LEGAL_NAME", raising=False)
        monkeypatch.delenv("HUB_OPERATOR_SUPPORT_CONTACT", raising=False)
        config = HubConfig.from_env()
        assert config.data_region == ""
        assert config.operator_legal_name == ""
        assert config.operator_support_contact == ""
