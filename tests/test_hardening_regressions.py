"""Regressions for review findings: revoked agents stay revoked, damaged catalog files
cannot fail a listing, publishing needs a version tag, and in-process embedders load
once per tag."""
import json
import os

import pytest

from commontrace import agent_registry, embeddings, gateway, kg_similarity, marketplace

TOKEN = "t" * 40
HEADERS = {"Host": "localhost:8787", "Authorization": "Bearer " + TOKEN}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def post(g, path, body):
    response = g.handle("POST", path, HEADERS, json.dumps(body).encode())
    return response.status, json.loads(response.body)


def test_rotating_a_revoked_agent_needs_an_explicit_reinstate(tmp_path):
    root = str(tmp_path)
    issued = agent_registry.signup(root, "bot-1")
    assert agent_registry.authenticate(root, issued["token"])
    assert agent_registry.revoke(root, "bot-1")["revoked"] is True
    assert agent_registry.authenticate(root, issued["token"]) is None
    with pytest.raises(ValueError, match="revoked"):
        agent_registry.rotate(root, "bot-1")
    assert agent_registry.load(root)["bot-1"]["revoked"] is True  # still revoked
    fresh = agent_registry.rotate(root, "bot-1", reinstate=True)
    assert agent_registry.authenticate(root, fresh["token"]) and not agent_registry.authenticate(root, issued["token"])
    assert agent_registry.load(root)["bot-1"]["reinstated_at"]
    with pytest.raises(ValueError, match="unknown agent"):
        agent_registry.revoke(root, "nobody")


def test_gateway_revoke_and_rotate_routes(tmp_path):
    root = str(tmp_path)
    g = gateway.Gateway(root, token=TOKEN)
    issued = agent_registry.signup(root, "bot-2")
    assert post(g, "/v1/agent/revoke", {"agent_id": "bot-2"})[0] == 200
    status, body = post(g, "/v1/agent/rotate", {"agent_id": "bot-2"})
    assert status == 400 and "revoked" in body["error"]["message"]
    assert post(g, "/v1/agent/rotate", {"agent_id": "bot-2", "reinstate": "yes"})[0] == 400
    assert post(g, "/v1/agent/rotate", {"agent_id": "bot-2", "reinstate": True})[0] == 200
    assert post(g, "/v1/agent/revoke", {"agent_id": "nobody"})[0] == 400
    # An agent's own key cannot reach owner routes.
    agent = gateway.Gateway(root, token=TOKEN).handle(
        "POST", "/v1/agent/revoke", {"Host": "localhost:8787", "Authorization": "Bearer " + issued["token"]},
        json.dumps({"agent_id": "bot-2"}).encode())
    assert agent.status in (401, 403)


def test_a_damaged_catalog_file_is_skipped(tmp_path):
    root = str(tmp_path)
    directory = marketplace._catalog_dir(root)
    os.makedirs(directory, exist_ok=True)
    for i, content in enumerate(("{not json", "[1, 2]", '{"record": "x"}')):
        with open(os.path.join(directory, f"{i:064x}.json"), "w", encoding="utf-8") as fh:
            fh.write(content)
    assert marketplace.catalog(root) == []
    response = gateway.Gateway(root, token=TOKEN).handle("GET", "/v1/market/listings", HEADERS, None)
    assert response.status == 200 and json.loads(response.body) == {"listings": []}


def test_release_publishes_only_from_a_version_tag():
    with open(os.path.join(REPO, ".github", "workflows", "release.yml"), encoding="utf-8") as fh:
        text = fh.read()
    conditions = [line.strip() for line in text.splitlines() if line.strip().startswith("if:") and "tags" in line]
    publish = [c for c in conditions if "dry_run" in c]
    assert len(publish) == 2
    for condition in publish:
        assert condition.startswith("if: startsWith(github.ref, 'refs/tags/v') &&"), condition


def test_in_process_embedders_load_once_per_tag(monkeypatch):
    built = []

    def provider(tag, *, local_files_only=False):
        built.append(tag)
        return object()

    monkeypatch.setattr(embeddings, "provider", provider)
    first = kg_similarity.embedder("fastembed:BAAI/bge-base-en-v1.5")
    assert kg_similarity.embedder("fastembed:BAAI/bge-base-en-v1.5") is first
    assert built == ["fastembed:BAAI/bge-base-en-v1.5"]
