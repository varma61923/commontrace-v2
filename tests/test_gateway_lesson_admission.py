"""Receipt and authoritative metadata checks survive warm gateway ranking/body caches."""
from __future__ import annotations

import os

import pytest

from commontrace import frontmatter, gateway, lesson_admission, lesson_io, workbench
from commontrace.cli import main
from tests.test_workbench import TOKEN, _lesson, call


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    _lesson(str(tmp_path), "lesson_good")
    return str(tmp_path)


def remember(gw, occasion: str):
    status, response = call(gw, "POST", "/v1/recall", {
        "occasion_id": occasion, "query": "payment retries stable idempotency key",
    })
    assert status == 200
    return response


@pytest.mark.parametrize("change", ["body", "core", "description", "provenance"])
def test_signed_lesson_tampering_is_withheld_after_warming_caches(root, change):
    workbench.approve(root, "lesson_good", None, "console")
    gw = gateway.Gateway(root, token=TOKEN)
    assert remember(gw, "warm")["deliver"][0]["id"] == "lesson_good"
    path = lesson_io.lesson_path(root, "lesson_good")
    fm, body = frontmatter.read(path)
    if change == "body":
        body += "\nUnreviewed ordinary guidance.\n"
    elif change == "core":
        fm["core"] = True
    elif change == "description":
        fm["description"] = "Unreviewed description."
    else:
        fm["llm_draft"] = {"model": "unreviewed producer"}
    frontmatter.write(path, fm, body)
    response = remember(gw, "tampered")
    assert response["deliver"] == [] and response["withheld"] == [] and response["protected"] == []


def test_durable_revocation_survives_old_active_file_restore(root):
    workbench.approve(root, "lesson_good", None, "console")
    gw = gateway.Gateway(root, token=TOKEN)
    assert remember(gw, "warm")["deliver"]
    path = lesson_io.lesson_path(root, "lesson_good")
    old = open(path, encoding="utf-8").read()
    lesson_admission.revoke(root, path, actor="console")
    assert remember(gw, "revoked")["deliver"] == []
    with open(path, "w", encoding="utf-8") as file:
        file.write(old)
    assert remember(gw, "restored")["deliver"] == []


def test_cached_description_is_replaced_by_verified_current_metadata(root):
    workbench.approve(root, "lesson_good", None, "console")
    gw = gateway.Gateway(root, token=TOKEN)
    assert remember(gw, "warm")["deliver"]
    active = gateway._ACTIVE_CACHE[os.path.abspath(root)][1]
    active[0][1]["description"] = "cached untrusted prose"
    response = remember(gw, "fresh-description")
    assert response["deliver"][0]["meta"]["description"] == "lesson_good description"
    assert "cached untrusted prose" not in str(response)


def test_cached_core_privilege_cannot_override_authoritative_core(root):
    workbench.approve(root, "lesson_good", None, "console")
    gw = gateway.Gateway(root, token=TOKEN)
    assert remember(gw, "warm")["deliver"]
    active = gateway._ACTIVE_CACHE[os.path.abspath(root)][1]
    active[0][1]["core"] = True
    response = remember(gw, "forged-core")
    assert response["deliver"] == [] and response["protected"] == []


def test_unsigned_legacy_file_still_supports_valid_updates(root):
    path = lesson_io.lesson_path(root, "lesson_good")
    fm, body = frontmatter.read(path)
    fm["status"] = "active"
    frontmatter.write(path, fm, body)
    gw = gateway.Gateway(root, token=TOKEN)
    assert remember(gw, "warm")["deliver"]
    frontmatter.write(path, {**fm, "core": True}, body + "\nLegacy update.\n")
    response = remember(gw, "legacy-updated")
    assert response["protected"] == ["lesson_good"]
    assert "Legacy update." in response["deliver"][0]["text"]
