"""Billing on proven value (commontrace/pricing.py): what it refuses, and the arithmetic of what it does not."""
import json
import os
import random

import pytest

from commontrace import functions, holdout_io, pricing, proof

KEY = b"0123456789abcdef-billing-test-key"
KIT = functions.from_dict({
    "key": "easy", "title": "Easy", "agent_type": "support", "occasion": {"label": "task", "example": "t-1"},
    "outcome": {"success": "the task finished", "window_days": 0, "signals": ["from_threshold"],
                "combine": "single"},
    "planning": {"baseline": 0.6, "effect": 0.15, "holdout_rate": 0.5}, "domains": ["general"]})
TERMS = {"currency": "USD", "platform_fee_per_agent_month": 100.0, "value_share": 0.2, "value_basis": "point_estimate",
         "cap_per_invoice": None, "minimum_invoice": 0.0, "credit_negative_value_against_platform_fee": False,
         "require_signature": True, "bill_interim": False}
SCHEDULE = pricing.PriceSchedule.from_dict(TERMS)


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: None)


def _grow(root, start, n, *, help_=0.2, harm=0.0, rates=None, seed=1):
    """Occasions `start`..`start+n`: `helps` +help_, `hurts` +harm (negative to hurt), `null` nothing."""
    config = holdout_io.load_config(root)
    rng = random.Random(f"{seed}:{start}")
    truth = {"helps": help_, "hurts": harm, "null": 0.0}
    slugs = list(truth)
    for i in range(start, start + n):
        slug = slugs[i % 3]
        withheld = holdout_io.assign_and_log(
            root, [slug], occasion_id=f"B-{i}", rate=(rates or {}).get(i, config.rate), salt=config.salt,
            revisions={slug: "r1"})
        holdout_io.record_outcome(root, f"B-{i}", rng.random() < 0.6 + (truth[slug] if slug not in withheld else 0))


def _store(tmp_path, name="s"):
    root = str(tmp_path / name)
    proof.start(root, KIT, label=f"acme-{name}", daily=40, value_per_occasion=50.0)
    return root


def _package(root, tmp_path, name, key=KEY):
    out = str(tmp_path / name)
    proof.build(root, out, key=key)
    return out


# --- The schedule has no default prices ----------------------------------------------------------------------


@pytest.mark.parametrize("drop", sorted(TERMS))
def test_every_commercial_term_is_required(drop):
    terms = {k: v for k, v in TERMS.items() if k != drop}
    with pytest.raises(pricing.PricingError, match=drop):
        pricing.PriceSchedule.from_dict(terms)


@pytest.mark.parametrize("change,message", [
    ({"value_share": 1.5}, "fraction"), ({"platform_fee_per_agent_month": -1}, "negative"),
    ({"value_basis": "mean"}, "value_basis"), ({"currency": " "}, "currency"),
    ({"require_signature": 1}, "wrong type"), ({"cap_per_invoice": -5}, "cap"), ({"surprise": 1}, "unknown")])
def test_a_bad_schedule_is_refused(change, message):
    with pytest.raises(pricing.PricingError, match=message):
        pricing.PriceSchedule.from_dict({**TERMS, **change})


def test_a_schedule_loads_from_the_owners_file(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(TERMS))
    assert pricing.PriceSchedule.from_file(str(path)).value_share == 0.2
    with pytest.raises(pricing.PricingError, match="cannot read"):
        pricing.PriceSchedule.from_file(str(tmp_path / "missing.json"))


# --- What is billed, and what is refused ---------------------------------------------------------------------


def test_a_verified_signed_final_package_is_billed_a_share_of_proven_value(tmp_path):
    root = _store(tmp_path)
    _grow(root, 0, 3000)
    pkg = _package(root, tmp_path, "p1")
    book = pricing._blank_book()
    inv = pricing.invoice(book, SCHEDULE, org="acme", period="2026-Q1", agent_months=6, packages=[pkg], key=KEY)
    kinds = [line.kind for line in inv.lines]
    assert kinds == ["platform", "value"] and not inv.refused
    value = next(line for line in inv.lines if line.kind == "value")
    record = json.load(open(os.path.join(pkg, "proof.json")))
    assert value.amount == pytest.approx(round(record["value"]["money"] * 0.2, 2))
    assert value.evidence["ledger_root"] == record["ledger_root"] and "proof verify" in value.evidence["verify"]
    assert inv.total == pytest.approx(600 + value.amount)


def test_the_lower_bound_basis_bills_less_than_the_point_estimate(tmp_path):
    root = _store(tmp_path)
    _grow(root, 0, 3000)
    pkg = _package(root, tmp_path, "p1")
    point = pricing.invoice(pricing._blank_book(), SCHEDULE, org="a", period="p", agent_months=0, packages=[pkg], key=KEY)
    low_schedule = pricing.PriceSchedule.from_dict({**TERMS, "value_basis": "ci_low"})
    low = pricing.invoice(pricing._blank_book(), low_schedule, org="a", period="p", agent_months=0, packages=[pkg],
                          key=KEY)
    assert 0 < low.total < point.total


def test_the_same_package_is_never_billed_twice(tmp_path):
    root = _store(tmp_path)
    _grow(root, 0, 3000)
    pkg = _package(root, tmp_path, "p1")
    book = pricing._blank_book()
    first = pricing.invoice(book, SCHEDULE, org="a", period="Q1", agent_months=0, packages=[pkg], key=KEY)
    again = pricing.invoice(book, SCHEDULE, org="a", period="Q2", agent_months=0, packages=[pkg], key=KEY)
    assert first.total > 0 and again.total == 0
    assert again.refused[0]["reason"] == "this package was already billed"


def test_a_period_is_billed_once(tmp_path):
    book = pricing._blank_book()
    pricing.invoice(book, SCHEDULE, org="a", period="Q1", agent_months=1, packages=[], key=KEY)
    with pytest.raises(pricing.PricingError, match="already has an invoice"):
        pricing.invoice(book, SCHEDULE, org="a", period="Q1", agent_months=1, packages=[], key=KEY)


def test_a_compromised_experiment_is_never_billed(tmp_path):
    root = _store(tmp_path)
    _grow(root, 0, 900, rates={i: 0.1 for i in range(300, 900)})        # two rates under one salt
    pkg = _package(root, tmp_path, "p1")
    inv = pricing.invoice(pricing._blank_book(), SCHEDULE, org="a", period="p", agent_months=0, packages=[pkg], key=KEY)
    assert inv.total == 0 and "COMPROMISED" in inv.refused[0]["reason"]


def test_synthetic_unsigned_interim_and_tampered_packages_are_refused(tmp_path):
    book = pricing._blank_book()
    demo = str(tmp_path / "demo")
    proof.start_demo(demo, KIT, value_per_occasion=50.0)
    demo_pkg = _package(demo, tmp_path, "demo-pkg")
    root = _store(tmp_path, "real")
    _grow(root, 0, 3000)
    unsigned = _package(root, tmp_path, "unsigned", key=None)
    signed = _package(root, tmp_path, "signed")
    tampered = str(tmp_path / "tampered")
    import shutil
    shutil.copytree(signed, tampered)
    csv_path = os.path.join(tampered, "assignments.csv")
    text = open(csv_path).read().replace(",true,", ",false,", 40)
    open(csv_path, "w").write(text)
    interim_root = _store(tmp_path, "interim")
    _grow(interim_root, 0, 40)
    interim = _package(interim_root, tmp_path, "interim-pkg")
    inv = pricing.invoice(book, SCHEDULE, org="a", period="p", agent_months=0,
                          packages=[demo_pkg, unsigned, tampered, interim], key=KEY, commit=False)
    reasons = {os.path.basename(r["package"]): r["reason"] for r in inv.refused}
    assert "synthetic" in reasons["demo-pkg"] and "not signed" in reasons["unsigned"]
    assert "does not verify" in reasons["tampered"] and "interim" in reasons["interim-pkg"]
    assert inv.total == 0 and not [line for line in inv.lines if line.kind == "value"]


def test_a_signature_is_required_to_be_checkable(tmp_path):
    root = _store(tmp_path)
    _grow(root, 0, 3000)
    pkg = _package(root, tmp_path, "p1")
    inv = pricing.invoice(pricing._blank_book(), SCHEDULE, org="a", period="p", agent_months=0, packages=[pkg],
                          key=None, commit=False)
    assert "no key was given" in inv.refused[0]["reason"]
    relaxed = pricing.PriceSchedule.from_dict({**TERMS, "require_signature": False})
    assert pricing.invoice(pricing._blank_book(), relaxed, org="a", period="p", agent_months=0, packages=[pkg],
                           key=None, commit=False).total > 0


def test_interim_runs_bill_only_when_the_schedule_says_so(tmp_path):
    root = _store(tmp_path)
    _grow(root, 0, 300)
    pkg = _package(root, tmp_path, "interim")
    record = json.load(open(os.path.join(pkg, "proof.json")))
    if record["final"]:
        pytest.skip("this sample already reached a final verdict")
    lenient = pricing.PriceSchedule.from_dict({**TERMS, "bill_interim": True})
    inv = pricing.invoice(pricing._blank_book(), lenient, org="a", period="p", agent_months=0, packages=[pkg],
                          key=KEY, commit=False)
    assert not any("interim" in r["reason"] for r in inv.refused)


# --- Cumulative billing, credits, cap and minimum --------------------------------------------------------------


def _tweak(book, experiment, billed):
    book["experiments"][experiment]["billed_basis"] = billed


def test_a_later_package_bills_only_the_increase(tmp_path):
    root = _store(tmp_path)
    _grow(root, 0, 1500)
    first = _package(root, tmp_path, "p1")
    book = pricing._blank_book()
    a = pricing.invoice(book, SCHEDULE, org="a", period="Q1", agent_months=0, packages=[first], key=KEY)
    _grow(root, 1500, 3000)
    second = _package(root, tmp_path, "p2")
    b = pricing.invoice(book, SCHEDULE, org="a", period="Q2", agent_months=0, packages=[second], key=KEY)
    basis2 = pricing.assess(second, SCHEDULE, KEY).basis
    # what was charged in total is exactly the share of what is proven now, never more
    assert a.total + b.total == pytest.approx(round(basis2 * 0.2, 2), abs=0.02)


def test_a_fall_in_proven_value_becomes_a_credit_and_the_sum_never_exceeds_the_share_of_the_latest(tmp_path):
    root = _store(tmp_path)
    _grow(root, 0, 3000)
    pkg = _package(root, tmp_path, "p1")
    book = pricing._blank_book()
    pricing.invoice(book, SCHEDULE, org="a", period="Q1", agent_months=0, packages=[pkg], key=KEY)
    experiment = next(iter(book["experiments"]))
    _tweak(book, experiment, book["experiments"][experiment]["billed_basis"] * 2)   # as if twice as much was billed
    book["experiments"][experiment]["ledger_roots"] = []
    again = pricing.invoice(book, SCHEDULE, org="a", period="Q2", agent_months=0, packages=[pkg], key=KEY)
    assert again.total == 0 and any(line.kind == "credit" for line in again.lines) and again.carried_credit > 0


def test_the_platform_fee_is_independent_of_any_result(tmp_path):
    inv = pricing.invoice(pricing._blank_book(), SCHEDULE, org="a", period="Q1", agent_months=30, packages=[], key=KEY)
    assert inv.total == 3000 and [line.kind for line in inv.lines] == ["platform"]


def test_a_cap_and_a_minimum_bound_the_invoice():
    capped = pricing.PriceSchedule.from_dict({**TERMS, "cap_per_invoice": 1000.0})
    assert pricing.invoice(pricing._blank_book(), capped, org="a", period="p", agent_months=50, packages=[],
                           key=KEY).total == 1000.0
    floor = pricing.PriceSchedule.from_dict({**TERMS, "minimum_invoice": 250.0})
    assert pricing.invoice(pricing._blank_book(), floor, org="a", period="p", agent_months=1, packages=[],
                           key=KEY).total == 250.0


def test_the_invoice_renders_with_how_to_check_each_value_line(tmp_path):
    root = _store(tmp_path)
    _grow(root, 0, 3000)
    pkg = _package(root, tmp_path, "p1")
    text = pricing.render(pricing.invoice(pricing._blank_book(), SCHEDULE, org="a", period="Q1", agent_months=3,
                                          packages=[pkg], key=KEY))
    assert "ledger root" in text and "commontrace proof verify" in text and "TOTAL" in text


# --- A year, month by month ---------------------------------------------------------------------------------------


def test_over_a_year_nothing_unproven_is_ever_billed_and_what_is_billed_equals_the_share_of_the_latest_proof(tmp_path):
    """Four experiments run through twelve months: one healthy and growing, one that goes COMPROMISED in
    month 5, one that never gathers enough data, one that is demo data. Quarterly invoices, then a check of the
    invariants over the whole year."""
    book = pricing._blank_book()
    healthy = _store(tmp_path, "healthy")
    broken = _store(tmp_path, "broken")
    thin = _store(tmp_path, "thin")
    demo = str(tmp_path / "demo")
    proof.start_demo(demo, KIT, value_per_occasion=50.0)
    bills = {}
    for quarter in range(1, 5):
        month = quarter * 3
        _grow(healthy, (quarter - 1) * 1000, 1000, seed=quarter)
        _grow(thin, (quarter - 1) * 6, 6, seed=quarter)
        _grow(broken, (quarter - 1) * 300, 300, rates={i: 0.1 for i in range(900, 1200)}, seed=quarter) \
            if quarter == 4 else _grow(broken, (quarter - 1) * 300, 300, seed=quarter)
        packages = [_package(healthy, tmp_path, f"h{quarter}"), _package(broken, tmp_path, f"b{quarter}"),
                    _package(thin, tmp_path, f"t{quarter}"), _package(demo, tmp_path, f"d{quarter}")]
        bills[quarter] = pricing.invoice(book, SCHEDULE, org="acme", period=f"2026-M{month}", agent_months=3 * 5,
                                         packages=packages, key=KEY)
    refused = [r for inv in bills.values() for r in inv.refused]
    # 1. nothing from the demo, the thin run, or the compromised one is ever a value line
    billed_labels = {line.evidence["label"] for inv in bills.values() for line in inv.lines if line.evidence}
    assert billed_labels <= {"acme-healthy", "acme-broken"}
    assert "acme-thin" not in billed_labels and "demo-easy" not in billed_labels
    assert any("COMPROMISED" in r["reason"] for r in refused)           # the broken run, once compromised
    assert any("synthetic" in r["reason"] for r in refused)
    # 2. the value charged for each experiment equals the share of the value basis last billed -- never more
    for experiment, state in book["experiments"].items():
        charged = sum(line.amount for inv in bills.values() for line in inv.lines
                      if line.evidence and line.evidence["experiment"] == experiment and line.kind in ("value",))
        credits = sum(line.amount for inv in bills.values() for line in inv.lines
                      if line.evidence and line.evidence["experiment"] == experiment and line.kind == "credit")
        assert charged + credits == pytest.approx(round(state["billed_basis"] * 0.2, 2), abs=0.05 * len(bills))
    # 3. the platform fee was charged every quarter regardless
    assert all(inv.platform_subtotal == 1500 for inv in bills.values())
    # 4. the book remembers four invoices and refuses to re-bill any of them
    assert len(book["invoices"]) == 4
    with pytest.raises(pricing.PricingError):
        pricing.invoice(book, SCHEDULE, org="acme", period="2026-M3", agent_months=1, packages=[], key=KEY)


# --- The command ------------------------------------------------------------------------------------------------


def test_the_template_has_every_term_empty_and_a_template_is_not_a_valid_schedule(tmp_path, capsys):
    from commontrace.cli import main
    assert main(["bill", "template"]) == 0
    out = capsys.readouterr().out
    terms = json.loads(out)
    assert set(terms) == set(TERMS) and all(v is None for v in terms.values())
    path = tmp_path / "t.json"
    path.write_text(out)
    with pytest.raises(pricing.PricingError):
        pricing.PriceSchedule.from_file(str(path))


def test_invoice_previews_by_default_and_records_only_with_commit(tmp_path, capsys):
    from commontrace.cli import main
    root = _store(tmp_path)
    _grow(root, 0, 3000)
    pkg = _package(root, tmp_path, "p1")
    sched = tmp_path / "prices.json"
    sched.write_text(json.dumps(TERMS))
    keyfile = tmp_path / "key"
    keyfile.write_bytes(KEY)
    book = tmp_path / "book"
    argv = ["bill", "invoice", "--schedule", str(sched), "--org", "acme", "--period", "2026-Q1", "--agent-months", "6",
            "--package", pkg, "--key-file", str(keyfile), "--book", str(book)]
    assert main(argv) == 0
    out = capsys.readouterr()
    assert "TOTAL" in out.out and "preview" in out.err and not os.path.exists(book / pricing.BOOK_NAME)
    assert main([*argv, "--commit"]) == 0
    assert os.path.isfile(book / pricing.BOOK_NAME) and os.path.isfile(book / "invoice-acme-2026-Q1.json")
    assert main([*argv, "--commit"]) == 2                      # the period is already billed
    assert "already has an invoice" in capsys.readouterr().err


def test_a_missing_or_incomplete_schedule_stops_the_command(tmp_path, capsys):
    from commontrace.cli import main
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"currency": "USD"}))
    assert main(["bill", "invoice", "--schedule", str(path), "--org", "a", "--period", "p", "--agent-months", "1"]) == 2
    assert "missing" in capsys.readouterr().err
