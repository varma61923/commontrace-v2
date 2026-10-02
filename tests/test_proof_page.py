import os
import random
import re

import pytest

from commontrace import functions, holdout_io, proof, proof_page
from commontrace.cli import main

SUPPORT = functions.builtin_kits()["support"]


def _run_fleet(root, n, *, rates=None, seed=1):
    config = holdout_io.load_config(root)
    rng = random.Random(seed)
    truth = {"helps": 0.2, "hurts": -0.25, "null": 0.0}
    slugs = list(truth)
    for i in range(n):
        slug = slugs[i % 3]
        withheld = holdout_io.assign_and_log(
            root, [slug], occasion_id=f"T-{i}", rate=(rates or {}).get(i, config.rate), salt=config.salt,
            revisions={slug: "r1"})
        holdout_io.record_outcome(root, f"T-{i}", rng.random() < 0.6 + (truth[slug] if slug not in withheld else 0))


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: None)


def _record(root, tmp_path, key=b"0123456789abcdef-test-signing-key"):
    return proof.build(root, str(tmp_path / "pkg"), key=key)


@pytest.fixture
def started(tmp_path):
    root = str(tmp_path / "store")
    proof.start(root, SUPPORT, label="Acme support", daily=300, value_per_occasion=12.0)
    return root


def test_the_package_includes_a_self_contained_page(started, tmp_path):
    _run_fleet(started, 900)
    record = _record(started, tmp_path)
    page = open(tmp_path / "pkg" / proof.PAGE_NAME, encoding="utf-8").read()
    assert page == proof_page.render(record)
    assert "Agent Learning Proof: Acme support" in page and "What each memory did" in page
    assert all(slug in page for slug in ("helps", "hurts", "null"))
    assert record["ledger_root"] in page and record["evidence"]["digest"] in page
    assert "<svg" in page and "<table" in page
    assert 'role="img"' in page


def test_the_page_has_no_script_and_no_external_request(started, tmp_path):
    _run_fleet(started, 900)
    _record(started, tmp_path)
    page = open(tmp_path / "pkg" / proof.PAGE_NAME, encoding="utf-8").read()
    assert not re.search(r"<script|src=|<link|@import|url\(", page)


def test_a_hostile_memory_name_is_text_never_markup(started, tmp_path):
    root = started
    config = holdout_io.load_config(root)
    rng = random.Random(0)
    evil = "<img src=x onerror=alert(1)>"
    for i in range(300):
        withheld = holdout_io.assign_and_log(root, [evil], occasion_id=f"E-{i}", rate=config.rate,
                                             salt=config.salt, revisions={evil: "r1"})
        holdout_io.record_outcome(root, f"E-{i}", rng.random() < (0.5 if evil in withheld else 0.7))
    _record(root, tmp_path)
    page = open(tmp_path / "pkg" / proof.PAGE_NAME, encoding="utf-8").read()
    assert "<img" not in page and "&lt;img src=x onerror=alert(1)&gt;" in page


def test_a_compromised_run_shows_no_figure_and_says_why(started, tmp_path):
    _run_fleet(started, 900, rates={i: 0.1 for i in range(300, 900)})
    record = _record(started, tmp_path)
    page = open(tmp_path / "pkg" / proof.PAGE_NAME, encoding="utf-8").read()
    assert record["integrity"]["verdict"] == "COMPROMISED"
    assert "no effect is stated" in page and "Why there is no figure" in page
    assert "What each memory did" not in page
    assert "Memories helping</div>\n  <div class=\"value\">–" in page


def test_an_interim_and_a_synthetic_run_are_labelled_on_their_face(tmp_path):
    root = str(tmp_path / "demo")
    proof.start_demo(root, SUPPORT)
    record = _record(root, tmp_path)
    assert "Synthetic demo data" in proof_page.render(record)
    started = str(tmp_path / "s")
    proof.start(started, SUPPORT, label="x", daily=300)
    _run_fleet(started, 30)
    interim = proof_page.render(proof.build(started, str(tmp_path / "p2"))
                                | {"final": False})
    assert "Interim" in interim


def test_the_signature_line_says_what_a_signature_does_and_does_not_establish(started, tmp_path):
    _run_fleet(started, 900)
    signed = proof_page.render(_record(started, tmp_path))
    unsigned = proof_page.render(_record(started, tmp_path, key=None))
    assert "Signed by the issuer" in signed and "Not signed" in unsigned
    assert "not that they were honest" in signed


EASY = {"key": "easy", "title": "Easy", "agent_type": "support", "occasion": {"label": "task", "example": "t-1"},
        "outcome": {"success": "the task finished", "window_days": 0, "signals": ["from_threshold"],
                    "combine": "single"},
        "planning": {"baseline": 0.5, "effect": 0.2, "holdout_rate": 0.5}, "domains": ["general"]}


@pytest.fixture
def easy_kit(tmp_path):
    import json
    path = tmp_path / "easy.json"
    path.write_text(json.dumps(EASY))
    return str(path)


def test_a_simulated_fleet_reaches_the_planted_verdicts_through_the_whole_wizard(easy_kit, tmp_path, capsys,
                                                                                 monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = str(tmp_path / "wiz")
    code = main(["proof", "wizard", easy_kit, "--label", "rehearsal", "--daily", "100", "--yes",
                 "--simulate", "--dest", store])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "sim-helpful-memory" in out and "HELPS" in out and "HURTS" in out
    assert "every verdict matches what was planted" in out and "package verifies: True" in out
    assert os.path.isfile(os.path.join("proof-rehearsal", proof.PAGE_NAME))


def test_the_wizard_without_a_tty_needs_its_answers_as_flags(easy_kit, tmp_path, capsys):
    assert main(["proof", "wizard", easy_kit, "--dest", str(tmp_path / "w")]) == 2
    assert "--label and --daily" in capsys.readouterr().err


def test_a_real_start_prints_how_to_connect_and_registers_before_data(easy_kit, tmp_path, capsys):
    store = str(tmp_path / "w")
    assert main(["proof", "wizard", easy_kit, "--label", "acme", "--daily", "100", "--yes",
                 "--dest", store]) == 0
    out = capsys.readouterr().out
    assert "registered before any data" in out and "commontrace gateway" in out
    assert proof.load_state(store)["label"] == "acme"
    assert not os.path.exists(holdout_io.holdout_log_path(store)) or \
        os.path.getsize(holdout_io.holdout_log_path(store)) == 0


def test_simulation_refuses_a_store_that_already_holds_assignments(tmp_path):
    root = str(tmp_path / "s")
    kit = functions.from_dict(EASY)
    proof.start(root, kit, label="x", daily=100)
    holdout_io.assign_and_log(root, ["m"], occasion_id="o1", rate=0.5,
                              salt=holdout_io.load_config(root).salt, revisions={"m": "r"})
    with pytest.raises(proof.ProofError, match="fresh store"):
        proof.simulate_fleet(root, kit)
