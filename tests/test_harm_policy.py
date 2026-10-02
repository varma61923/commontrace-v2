from __future__ import annotations

import json
import os

import pytest

from commontrace import evidence, experiment, harm, holdout_io, receipts, retrieval_io
from tests.test_mcp_server import _write_lesson, call, cli

pytest.importorskip("mcp", reason="`commontrace serve` needs the MCP SDK: pip install 'commontrace[serve]'")

from commontrace import mcp_server  # noqa: E402  (needs the SDK)

TASK = "customer asks for a password reset email"
BAD = "reset-link-in-chat"
GOOD = "verify-account-owner"
FILLER = [
    ("pin-dependency-versions", "pin dependency versions in the lockfile before a deploy"),
    ("drain-node-before-upgrade", "drain the kubernetes node before a kernel upgrade"),
    ("backfill-in-batches", "run a database backfill in small batches off peak"),
    ("rotate-api-keys", "rotate api keys quarterly and revoke the old ones"),
]


@pytest.fixture(autouse=True)
def _fresh_cache():
    evidence._cache.clear()
    yield
    evidence._cache.clear()


def _run(server, root, n, *, report, start=0, top_k=5):
    for i in range(start, start + n):
        occ = f"occ-{i}"
        out = call(server, "retrieve", task=TASK, occasion_id=occ, top_k=top_k)
        injected = {item["slug"] for item in out["lessons"]}
        outcome = report(i, injected)
        if outcome is not None:
            holdout_io.record_outcome(root, occ, outcome)


FIXED_SALT = "test-harm-policy-fixed-salt"


def _store(tmp_path, *, n, report):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "support").returncode == 0
    _write_lesson(
        root, BAD, importance=5,
        description="customer asks for a password reset email: paste the reset link into the chat",
        body="When a customer asks for a password reset email, paste the reset link "
             "straight into the chat so they do not have to wait for the email.",
    )
    _write_lesson(
        root, GOOD, importance=2,
        description="password reset email: verify the account owner first",
        body="Before sending a password reset email, verify the account owner.",
    )
    for slug, topic in FILLER:
        _write_lesson(root, slug, description=topic, body=f"{topic}.")
    holdout_io.configure(root, rate=0.5)
    path = holdout_io.config_path(root)
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)
    config["salt"] = FIXED_SALT
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    server = mcp_server.build_server(root)
    _run(server, root, n, report=report)
    return root, server


@pytest.fixture
def harmed(tmp_path):
    return _store(tmp_path, n=160, report=lambda i, injected: BAD not in injected)


def _by_slug(out, name):
    return {item["slug"]: item for item in out.get(name) or []}


def _set_policy(root, policy):
    retrieval_io.configure(root, harm_policy=policy)


def test_the_fixture_establishes_harm_on_the_anytime_valid_boundary(harmed):
    root, _server = harmed
    by_lesson = evidence.for_lessons(root)["by_lesson"]
    assert by_lesson[BAD]["verdict"] == experiment.VERDICT_HURTS
    assert by_lesson[GOOD]["verdict"] != experiment.VERDICT_HURTS


def test_by_default_a_harmful_lesson_is_still_injected_with_its_verdict(harmed):
    root, server = harmed
    assert retrieval_io.load_config(root).harm_policy == harm.POLICY_INFORM
    out = call(server, "retrieve", task=TASK)
    assert BAD in _by_slug(out, "lessons")
    assert _by_slug(out, "lessons")[BAD]["evidence"]["verdict"] == experiment.VERDICT_HURTS
    assert "withdrawn" not in out


def test_withdraw_keeps_it_out_names_it_and_backfills_the_slot(harmed):
    root, server = harmed
    assert set(_by_slug(call(server, "retrieve", task=TASK, top_k=1), "lessons")) == {BAD}
    _set_policy(root, harm.POLICY_WITHDRAW)

    out = call(server, "retrieve", task=TASK, top_k=1, occasion_id="after-1")

    handed_over = {*_by_slug(out, "lessons"), *_by_slug(out, "withheld")}
    assert handed_over == {GOOD}
    withdrawn = _by_slug(out, "withdrawn")
    assert set(withdrawn) == {BAD}
    assert withdrawn[BAD]["reason"] == harm.REASON
    assert withdrawn[BAD]["evidence"]["verdict"] == experiment.VERDICT_HURTS
    assert withdrawn[BAD]["evidence"]["effect"] < 0
    assert "body" not in withdrawn[BAD]
    assert "HURTS" in out["withdrawn_note"]


def test_a_withdrawn_lesson_is_never_assigned_an_arm(harmed):
    root, server = harmed
    _set_policy(root, harm.POLICY_WITHDRAW)
    call(server, "retrieve", task=TASK, occasion_id="after-arm")

    with open(holdout_io.holdout_log_path(root), encoding="utf-8") as fh:
        logged = [json.loads(line) for line in fh if line.strip()]
    for_occasion = {row["lesson"] for row in logged if row["occasion_id"] == "after-arm"}
    assert GOOD in for_occasion
    assert BAD not in for_occasion


def test_the_receipt_says_why_it_was_not_injected(harmed):
    root, server = harmed
    _set_policy(root, harm.POLICY_WITHDRAW)
    call(server, "retrieve", task=TASK, occasion_id="after-receipt")
    receipt = [r for r in receipts.read_all(root) if r.occasion_id == "after-receipt"][-1]
    assert (BAD, "measured harm (withdrawn)") in receipt.withheld


def test_every_other_lesson_ranks_exactly_as_before(harmed):
    root, server = harmed
    before = call(server, "retrieve", task=TASK)
    _set_policy(root, harm.POLICY_WITHDRAW)
    after = call(server, "retrieve", task=TASK)
    assert _by_slug(after, "lessons")[GOOD]["score"] == _by_slug(before, "lessons")[GOOD]["score"]


def test_the_experiment_stays_readable_and_the_verdict_stands(harmed):
    root, server = harmed
    _set_policy(root, harm.POLICY_WITHDRAW)
    _run(server, root, 60, start=1000, report=lambda i, injected: i % 3 != 0)

    status = call(server, "experiment_status")
    assert status["integrity"]["effects_readable"] is True
    verdicts = {e["lesson_slug"]: e["verdict"] for e in status["effects"]}
    assert verdicts[BAD] == experiment.VERDICT_HURTS


def test_only_an_anytime_valid_verdict_is_acted_on(tmp_path):
    root, server = _store(
        tmp_path, n=160,
        report=lambda i, injected: (i % 5) < (2 if BAD in injected else 3),
    )
    rows = evidence.analyse(root).rows
    from commontrace.commands import experiment_cmd

    fixed = {e.lesson_slug: e.verdict for e in experiment.analyze(experiment_cmd._observations(rows))}
    assert fixed[BAD] == experiment.VERDICT_HURTS

    assert evidence.for_lessons(root)["by_lesson"][BAD]["verdict"] == experiment.VERDICT_UNDERPOWERED
    _set_policy(root, harm.POLICY_WITHDRAW)
    out = call(server, "retrieve", task=TASK)
    assert BAD in _by_slug(out, "lessons")
    assert "withdrawn" not in out


def test_a_new_randomization_gives_it_a_second_trial(harmed):
    root, server = harmed
    _set_policy(root, harm.POLICY_WITHDRAW)
    holdout_io.configure(root, rate=0.5)
    out = call(server, "retrieve", task=TASK)
    assert BAD in _by_slug(out, "lessons")
    assert "withdrawn" not in out


def test_a_compromised_experiment_withdraws_nothing(harmed, monkeypatch):
    root, server = harmed
    _set_policy(root, harm.POLICY_WITHDRAW)
    monkeypatch.setattr(evidence, "for_lessons", lambda _root: {
        "available": False, "reason": "COMPROMISED", "measured_at": "", "by_lesson": {},
    })
    assert evidence.withdrawn(root, harm.POLICY_WITHDRAW) == {}
    out = call(server, "retrieve", task=TASK)
    assert BAD in _by_slug(out, "lessons")


def test_unreadable_evidence_never_stops_retrieval(harmed, monkeypatch):
    root, server = harmed
    _set_policy(root, harm.POLICY_WITHDRAW)

    def boom(_root):
        raise OSError("disk went away")

    monkeypatch.setattr(evidence, "for_lessons", boom)
    assert evidence.withdrawn(root, harm.POLICY_WITHDRAW) == {}


def test_query_withdraws_the_same_lesson_and_says_so(harmed):
    root, _server = harmed
    assert cli("retrieval", "--on-harm", "withdraw", "--dest", root).returncode == 0

    out = cli("query", "--lexical", "--dest", root, TASK)
    assert out.returncode == 0, out.stderr
    listed = [line.split()[0] for line in out.stdout.splitlines()
              if line and not line.startswith(("[", " ", "\n"))]
    assert BAD not in listed
    assert GOOD in listed
    assert "withdrawn -- measured to make outcomes worse" in out.stdout
    assert BAD in out.stdout.split("withdrawn --", 1)[1]


def test_query_never_assigns_a_withdrawn_lesson_an_arm(harmed):
    root, _server = harmed
    assert cli("retrieval", "--on-harm", "withdraw", "--dest", root).returncode == 0
    out = cli("query", "--lexical", "--experiment", "--occasion-id", "cli-occ",
              "--dest", root, TASK)
    assert out.returncode == 0, out.stderr
    with open(holdout_io.holdout_log_path(root), encoding="utf-8") as fh:
        logged = {json.loads(line)["lesson"] for line in fh
                  if line.strip() and json.loads(line)["occasion_id"] == "cli-occ"}
    assert logged == {GOOD}


def test_the_setting_persists_and_survives_other_settings_changing(tmp_path):
    root = str(tmp_path / "s")
    os.makedirs(root)
    retrieval_io.configure(root, harm_policy=harm.POLICY_WITHDRAW)
    retrieval_io.configure(root, max_lessons=3)
    assert retrieval_io.load_config(root).harm_policy == harm.POLICY_WITHDRAW
    retrieval_io.configure(root, harm_policy=harm.POLICY_INFORM)
    assert retrieval_io.load_config(root).harm_policy == harm.POLICY_INFORM


def test_an_unknown_setting_is_refused_on_write_and_ignored_on_read(tmp_path):
    root = str(tmp_path / "s")
    os.makedirs(root)
    with pytest.raises(ValueError):
        retrieval_io.configure(root, harm_policy="delete")
    retrieval_io.configure(root, max_lessons=3)
    path = retrieval_io.config_path(root)
    raw = json.loads(open(path, encoding="utf-8").read())
    raw["harm_policy"] = ["withdraw"]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)
    assert retrieval_io.load_config(root).harm_policy == harm.POLICY_INFORM


def test_inform_never_reads_the_evidence(tmp_path, monkeypatch):
    def boom(_root):
        raise AssertionError("evidence read under the inform policy")

    monkeypatch.setattr(evidence, "for_lessons", boom)
    assert evidence.withdrawn(str(tmp_path), harm.POLICY_INFORM) == {}


class TestSplit:
    def test_it_backfills_from_the_over_fetch(self):
        kept, removed = harm.split(["a", "b", "c", "d"], {"a"}, set(), 2, slug_of=lambda s: s)
        assert kept == ["b", "c"]
        assert removed == ["a"]

    def test_a_core_lesson_is_never_withdrawn(self):
        kept, removed = harm.split(["a", "b"], {"a"}, {"a"}, 2, slug_of=lambda s: s)
        assert kept == ["a", "b"]
        assert removed == []

    def test_one_below_the_cut_is_not_named(self):
        kept, removed = harm.split(["b", "c", "a"], {"a"}, set(), 2, slug_of=lambda s: s)
        assert kept == ["b", "c"]
        assert removed == []
