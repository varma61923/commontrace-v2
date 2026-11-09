from __future__ import annotations

import random

import pytest

from unimem import aipw, holdout_io, retention


def test_rewrite_blob_staging_cannot_follow_a_symlink_outside_the_store(tmp_path):
    import hashlib
    from pathlib import Path

    root = str(tmp_path / "store")
    payload = "Approved private rewrite."
    digest = hashlib.sha256(payload.encode()).hexdigest()
    target = Path(retention._blob_path(root, digest))
    target.parent.mkdir(parents=True)
    outside = tmp_path / "unrelated.txt"
    outside.write_text("untouched")
    target.with_suffix(".txt.partial").symlink_to(outside)
    retention._put_blob(root, payload)
    assert outside.read_text() == "untouched"
    assert target.read_text() == payload


def test_concurrent_rewrite_blob_writers_do_not_share_a_staging_file(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    root = str(tmp_path)
    ready = Barrier(2)
    replace = retention.os.replace

    def simultaneous(source, target):
        ready.wait(timeout=5)
        replace(source, target)

    monkeypatch.setattr(retention.os, "replace", simultaneous)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: retention._put_blob(root, "same private original"), range(2)))
    assert results[0] == results[1]


def test_rewrite_blobs_are_owner_only(tmp_path):
    from pathlib import Path

    root = str(tmp_path)
    digest = retention._put_blob(root, "Private original.")
    path = Path(retention._blob_path(root, digest))
    path.chmod(0o644)  # A file created by an older release or a permissive umask.
    retention._put_blob(root, "Private original.")
    assert path.stat().st_mode & 0o077 == 0


def test_failed_blob_replacement_cleans_staging_and_preserves_committed_text(tmp_path, monkeypatch):
    from pathlib import Path

    root = str(tmp_path)
    digest = retention._put_blob(root, "Committed original.")
    existing = Path(retention._blob_path(root, digest))

    def unavailable(*_):
        raise OSError("replace unavailable")

    monkeypatch.setattr(retention.os, "replace", unavailable)
    with pytest.raises(OSError, match="replace unavailable"):
        retention._put_blob(root, "Uncommitted replacement.")
    assert list(existing.parent.iterdir()) == [existing]
    assert existing.read_text() == "Committed original."


def proposals(root, slugs=("a", "b")):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import _write_lesson

    holdout_io.configure(root, rate=0.5, salt="rewrite-erasure")
    result = []
    for slug in slugs:
        _write_lesson(root, slug, body="Use the shared original safely.")
        row = retention.propose(root, slug, "Use the same approved rewrite.", author="author")["record"]
        retention.review(root, row["id"], reviewer="reviewer")
        result.append(row)
    return result


def test_erasure_preserves_other_proposals_shared_blobs_but_revokes_the_target(tmp_path):
    from pathlib import Path

    root = str(tmp_path)
    first, second = proposals(root)
    assert first["original_digest"] == second["original_digest"]
    assert first["body_digest"] == second["body_digest"]
    assert set(retention.trials(root)) == {"a", "b"}
    assert retention.erase_rewrites(root, {"a"}) == []
    assert set(retention.trials(root)) == {"b"}
    with pytest.raises(ValueError, match="erased"):
        retention.choose(root, first["id"], "erased")
    with pytest.raises(ValueError, match="erased"):
        retention.review(root, first["id"], reviewer="new-reviewer")
    assert retention.choose(root, second["id"], "retained")["body"]
    assert set(retention.erase_rewrites(root, {"b"})) == {second["original_digest"], second["body_digest"]}
    assert not Path(retention._blob_path(root, second["original_digest"])).exists()
    assert not Path(retention._blob_path(root, second["body_digest"])).exists()


def test_erasure_redacts_legacy_inline_text_from_the_signed_journal(tmp_path):
    from unimem import frontier_protocol as fp

    root = str(tmp_path)
    fp.append(
        root,
        retention.REWRITES,
        {
            "kind": "proposal",
            "id": "legacy",
            "slug": "a",
            "author": "author",
            "original": "sensitive original content",
            "body": "sensitive rewritten content",
        },
    )
    retention.erase_rewrites(root, {"a"})
    with open(fp.filename(root, retention.REWRITES), encoding="utf-8") as stream:
        raw = stream.read()
    assert "sensitive original content" not in raw and "sensitive rewritten content" not in raw
    assert any(row.get("kind") == "erased" and row["id"] == "legacy" for row in fp.journal(root, retention.REWRITES))
    with pytest.raises(ValueError, match="erased"):
        retention.choose(root, "legacy", "replay")


def test_withdrawn_source_cannot_be_served_under_an_old_rewrite_approval(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from unimem import evidence, experiment, lesson_admission, lesson_io

    root = str(tmp_path)
    [proposal] = proposals(root, ("a",))
    analysis = SimpleNamespace(
        report=SimpleNamespace(readable=True),
        effects=[SimpleNamespace(lesson_slug=proposal["id"], verdict=experiment.VERDICT_HELPS)],
    )
    monkeypatch.setattr(evidence, "analyse", lambda _: analysis)
    assert retention.selected(root, "a") is not None
    lesson_admission.revoke(root, lesson_io.lesson_path(root, "a"), actor="operator")
    assert retention.selected(root, "a") is None
    assert retention.overlay(root, "a", "new-occasion") is None
    with pytest.raises(ValueError, match="no longer admitted"):
        retention.choose(root, proposal["id"], "direct")


def test_concurrent_identical_proposals_have_one_durable_identity(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    pytest.importorskip("mcp")
    from tests.test_mcp_server import _write_lesson
    from unimem import frontier_protocol as fp

    root = str(tmp_path)
    _write_lesson(root, "a", body="Use the shared original safely.")
    ready = Barrier(4)

    def propose(_):
        ready.wait(timeout=5)
        return retention.propose(root, "a", "Use the same approved rewrite.", author="author")["record"]

    with ThreadPoolExecutor(max_workers=4) as workers:
        result = list(workers.map(propose, range(4)))
    assert all(row == result[0] for row in result)
    assert len([row for row in fp.journal(root, retention.REWRITES) if row["kind"] == "proposal"]) == 1


@pytest.mark.parametrize("recovery", ["retry", "erase"])
def test_interrupted_proposal_tracks_plaintext_for_retry_or_erasure(tmp_path, monkeypatch, recovery):
    from pathlib import Path

    pytest.importorskip("mcp")
    from tests.test_mcp_server import _write_lesson
    from unimem import frontier_protocol as fp

    root = str(tmp_path)
    _write_lesson(root, "a", body="Private original.")
    put = retention._put_blob
    calls = []

    def unavailable(root, text):
        calls.append(text)
        if len(calls) == 2:
            raise OSError("body storage unavailable")
        return put(root, text)

    monkeypatch.setattr(retention, "_put_blob", unavailable)
    with pytest.raises(OSError, match="body storage unavailable"):
        retention.propose(root, "a", "Private replacement.", author="author")
    [row] = [r for r in fp.journal(root, retention.REWRITES) if r["kind"] == "proposal"]
    original = Path(retention._blob_path(root, row["original_digest"]))
    assert original.exists()
    with pytest.raises(ValueError):
        retention.review(root, row["id"], reviewer="reviewer")
    assert retention.trials(root) == {}
    if recovery == "erase":
        assert retention.erase_rewrites(root, {"a"}) == [row["original_digest"]]
        assert not original.exists()
    else:
        monkeypatch.setattr(retention, "_put_blob", put)
        assert retention.propose(root, "a", "Private replacement.", author="author")["record"] == row
        retention.review(root, row["id"], reviewer="reviewer")
        assert retention.trials(root)["a"][0]["id"] == row["id"]


def test_seeded_randomized_utility_beats_judgment_only_budget():
    rng = random.Random(10)
    observations, features = [], []
    for i in range(1600):
        good = i % 2 == 0
        action = rng.random() < 0.5
        success = rng.random() < ((0.9 if good else 0.1) if action else 0.5)
        observations.append(aipw.Observation(action, success, 0.5, "good" if good else "harmful"))
        features.append([1, 1 if good else -1])
    model = retention.fit(observations, features)
    good = model.predict([1, 1], local_n=800)
    bad = model.predict([1, -1], local_n=800)
    assert good > 0.15 and bad < -0.15
    report = retention.dream_budget(
        [
            {"id": "good", "effect": good, "future_retrieval": 0.5, "tokens": 100},
            {"id": "bad", "effect": bad, "future_retrieval": 0.9, "tokens": 100},
            {"id": "hazard", "effect": 1, "future_retrieval": 1, "tokens": 1},
        ],
        tokens=100,
        protected={"hazard"},
    )
    assert report["changes"][0]["id"] == "good" and report["quarantine"] == ["bad"]
    assert good > bad  # A judgment-only policy choosing the frequent harmful item loses planted utility.


def test_rewrite_is_randomized_and_losing_rewrite_rolls_back(tmp_path):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import _write_lesson

    root = str(tmp_path)
    _write_lesson(root, "rule", body="Verify the account owner before resetting a password.")
    holdout_io.configure(root, rate=0.5, salt="rewrite-trial")
    proposal = retention.propose(root, "rule", "Skip account verification when resetting a password.", author="dreamer")
    identity = proposal["record"]["id"]
    retention.review(root, identity, reviewer="operator")
    # Only this two-arm trial is logged: true means rewrite, false original.
    for i in range(100):
        choice = retention.choose(root, identity, str(i))
        holdout_io.record_outcome(root, str(i), choice["arm"] == "original")
        if choice["rollback"]:
            break
    assert retention.choose(root, identity, "final")["rollback"]
    assert retention.selected(root, "rule") is None


def test_causal_dream_cli(tmp_path):
    import json

    pytest.importorskip("mcp")

    from tests.test_mcp_server import cli

    out = cli("dream", "--causal", "--dest", str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["changes"] == []


def test_retrieve_randomizes_reviewed_rewrites_and_replay_recovers_them(tmp_path):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import _write_lesson, call
    from unimem import frontier_protocol as fp
    from unimem import incident
    from unimem.mcp_server import build_server

    root = str(tmp_path)
    original = "Verify the account owner before resetting a password."
    _write_lesson(root, "reset", body=original)
    holdout_io.configure(root, rate=0.01, salt="rewrites")
    rewrite = "Before any password reset, confirm the account owner's identity with two factors."
    proposal = retention.propose(root, "reset", rewrite, author="dreamer")
    server = build_server(root)
    first = call(server, "retrieve", task="password reset account owner", occasion_id="pre-review")
    assert all(item["body"].strip() == original for item in first["lessons"])  # unreviewed: never served
    retention.review(root, proposal["record"]["id"], reviewer="operator")
    arms = {}
    for i in range(40):
        result = call(server, "retrieve", task="password reset account owner", occasion_id=f"o{i}")
        for item in result["lessons"]:
            arms[f"o{i}"] = item.get("rewrite", {}).get("arm", "original")
            assert item["body"].strip() == (rewrite if arms[f"o{i}"] == "rewrite" else original)
    assert set(arms.values()) == {"original", "rewrite"}
    served = next(occasion for occasion, arm in arms.items() if arm == "rewrite")
    incident.freeze(root, "rewrite-incident", fp.now())
    replay = incident.replay(root, "rewrite-incident", served)
    assert [row["body"] for row in replay["injected"]] == [rewrite]
