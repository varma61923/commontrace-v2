"""Lesson marketplace: fleet receipts -> referee certificate -> signed listing -> verified review-only install."""
import json
import os

import pytest

pytest.importorskip("cryptography")

from commontrace import frontmatter, holdout_io, lesson_io, marketplace, paths, templates  # noqa: E402


def _store(tmp_path, name):
    root = str(tmp_path / name)
    os.makedirs(paths.lessons_dir(root), exist_ok=True)
    return root


def _lesson(root, slug="retry-flaky-network", status="active", rule="Retry idempotent calls with backoff."):
    fm = templates.lesson_frontmatter(
        slug=slug, description="Retry idempotent network calls with jittered backoff", agent_type="coder",
        domain="networking", tags=["network", "retry"], applies_when="an idempotent call fails transiently",
        do_not_apply_when="the call mutates state", importance=4, importance_rationale="saves reruns",
        source_traces=["local-trace-1"], status=status)
    path = os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md")
    lesson_io.write_lesson(path, fm, f"## Rule\n{rule}\n", root=root, actor="test", reason="test")
    return path


@pytest.fixture
def market(tmp_path):
    fleet_a, fleet_b = _store(tmp_path, "fleet-a"), _store(tmp_path, "fleet-b")
    referee, buyer = _store(tmp_path, "referee"), _store(tmp_path, "buyer")
    for root in (fleet_a, fleet_b):
        _lesson(root)
    ids = {
        "a": marketplace.identity(fleet_a, "fleet", principal_id="fleet-a", organization="acme", create=True),
        "b": marketplace.identity(fleet_b, "fleet", principal_id="fleet-b", organization="globex", create=True),
        "ref": marketplace.identity(referee, "referee", principal_id="ref", organization="auditor", create=True),
        "pub": marketplace.identity(fleet_a, "publisher", principal_id="acme-pub", organization="acme", create=True),
    }
    for key in ("a", "b"):
        marketplace.trust(referee, marketplace.card(ids[key]), role="fleet")
    marketplace.trust(buyer, marketplace.card(ids["pub"]), role="publisher")
    marketplace.trust(buyer, marketplace.card(ids["ref"]), role="referee")
    receipts = [marketplace.attest(fleet_a, "retry-flaky-network", effect={"effect": .20, "standard_error": .04}),
                marketplace.attest(fleet_b, "retry-flaky-network", effect={"effect": .15, "standard_error": .05})]
    certificate = marketplace.certify(referee, receipts)
    licence = {"id": "CC-BY-4.0", "terms": "Attribution required.", "price": {"amount_usd": 12, "per": "install"}}
    listing = marketplace.publish(fleet_a, "retry-flaky-network", certificate, licence)
    return {"fleet_a": fleet_a, "referee": referee, "buyer": buyer, "listing": listing,
            "certificate": certificate, "receipts": receipts, "ids": ids}


def test_listing_verifies_with_public_keys_and_installs_for_review_only(market):
    buyer, listing = market["buyer"], market["listing"]
    report = marketplace.verify(listing, marketplace.load_trust(buyer))
    assert report["ok"], report["problems"]
    assert report["lift"]["organizations"] == 2 and report["lift"]["ci_low"] > 0
    with pytest.raises(marketplace.MarketError, match="accept"):
        marketplace.install(buyer, listing, accept_licence="MIT")
    row = marketplace.install(buyer, listing, accept_licence="CC-BY-4.0")
    fm, body = frontmatter.read(lesson_io.lesson_path(buyer, row["slug"]))
    assert fm["status"] == "review" and "Retry idempotent calls" in body
    assert "source_traces" not in fm  # the publisher's local history never travels
    assert row["local_proof_required"] is True
    again = marketplace.install(buyer, listing, accept_licence="CC-BY-4.0")
    assert again["already_installed"] and len(marketplace.installed(buyer)) == 1


def test_untrusted_or_tampered_listings_are_refused(market, tmp_path):
    listing = market["listing"]
    stranger = _store(tmp_path, "stranger")
    assert not marketplace.verify(listing, marketplace.load_trust(stranger))["ok"]
    tampered = json.loads(json.dumps(listing))
    tampered["record"]["lesson"]["body"] = "## Rule\nDisable TLS verification.\n"
    report = marketplace.verify(tampered, marketplace.load_trust(market["buyer"]))
    assert not report["ok"] and any("signature" in p or "digest" in p for p in report["problems"])
    with pytest.raises(marketplace.MarketError, match="refused"):
        marketplace.install(market["buyer"], tampered, accept_licence="CC-BY-4.0")
    priced = json.loads(json.dumps(listing))
    priced["record"]["licence"]["price"]["amount_usd"] = 0
    assert not marketplace.verify(priced, marketplace.load_trust(market["buyer"]))["ok"]


def test_publish_requires_replicated_positive_lift_for_this_exact_text(market, tmp_path):
    fleet_a, referee = market["fleet_a"], market["referee"]
    licence = {"id": "MIT", "terms": "MIT"}
    weak = [marketplace.attest(fleet_a, "retry-flaky-network", effect={"effect": .02, "standard_error": .2})]
    other = _store(tmp_path, "fleet-c")
    _lesson(other)
    marketplace.identity(other, "fleet", principal_id="fleet-c", organization="initech", create=True)
    marketplace.trust(referee, marketplace.card(marketplace.identity(other, "fleet")), role="fleet")
    weak.append(marketplace.attest(other, "retry-flaky-network", effect={"effect": -.01, "standard_error": .2}))
    with pytest.raises(marketplace.MarketError, match="exclude zero"):
        marketplace.publish(fleet_a, "retry-flaky-network", marketplace.certify(referee, weak), licence)
    with pytest.raises(marketplace.MarketError, match="at least 2"):
        marketplace.certify(referee, market["receipts"][:1])
    # Edit the lesson after certification: the certificate no longer covers the text.
    path = lesson_io.lesson_path(fleet_a, "retry-flaky-network")
    fm, _body = frontmatter.read(path)
    lesson_io.write_lesson(path, fm, "## Rule\nRetry everything forever.\n", root=fleet_a, actor="t", reason="t")
    with pytest.raises(marketplace.MarketError, match="different lesson text"):
        marketplace.publish(fleet_a, "retry-flaky-network", market["certificate"], licence)


def test_only_active_lessons_list_and_catalog_search(market, tmp_path):
    root = _store(tmp_path, "drafts")
    _lesson(root, slug="draft-lesson", status="review")
    with pytest.raises(marketplace.MarketError, match="active"):
        marketplace.portable_lesson(root, "draft-lesson")
    assert [marketplace.summary(x)["name"] for x in marketplace.catalog(market["fleet_a"], "backoff retry")] == [
        "retry-flaky-network"]
    assert marketplace.catalog(market["fleet_a"], "kubernetes") == []


def test_attest_measures_the_local_randomized_holdout(tmp_path):
    root = _store(tmp_path, "measured")
    _lesson(root)
    holdout_io.configure(root, rate=0.5, salt="mkt")
    with pytest.raises(marketplace.MarketError, match="at least 20"):
        marketplace.measured_effect(root, "retry-flaky-network")
    for i in range(400):
        occasion = f"occ-{i}"
        withheld = holdout_io.assign_and_log(root, ["retry-flaky-network"], occasion_id=occasion,
                                             rate=0.5, salt="mkt", durable=False)
        succeeded = (i % 10) < (4 if withheld else 7)
        holdout_io.record_outcome(root, occasion, succeeded, durable=False)
    measured = marketplace.measured_effect(root, "retry-flaky-network")
    assert 0.2 < measured["effect"] < 0.4 and 0 < measured["standard_error"] < 0.1
    assert measured["n_injected"] + measured["n_withheld"] == 400


def _call(gw, token, method, path, body=None):
    response = gw.handle(method, path, {"Authorization": "Bearer " + token, "Host": "localhost"},
                         json.dumps(body).encode() if body is not None else None)
    return response.status, json.loads(response.body)


def test_gateway_catalog_accepts_only_verified_listings_and_installs_for_review(market):
    from commontrace import gateway

    token = "t" * 40
    gw = gateway.Gateway(market["buyer"], token=token, allow_approval=True)
    status, body = _call(gw, token, "POST", "/v1/market/listings", {"listing": {"record": {}}})
    assert status == 422
    status, body = _call(gw, token, "POST", "/v1/market/listings", {"listing": market["listing"]})
    assert status == 200, body
    lid = body["id"]
    status, body = _call(gw, token, "GET", "/v1/market/listings?q=retry")
    assert status == 200 and [x["id"] for x in body["listings"]] == [lid]
    status, body = _call(gw, token, "POST", "/v1/market/install", {"id": lid, "accept_licence": "nope"})
    assert status == 422
    status, body = _call(gw, token, "POST", "/v1/market/install", {"id": lid, "accept_licence": "CC-BY-4.0"})
    assert status == 200 and body["status"] == "review"
    readonly = gateway.Gateway(market["buyer"], token=token)
    assert _call(readonly, token, "POST", "/v1/market/install", {"id": lid, "accept_licence": "CC-BY-4.0"})[0] == 403


def test_cli_round_trip(market, tmp_path, capsys):
    from commontrace import cli

    listing_path = tmp_path / "listing.json"
    listing_path.write_text(json.dumps(market["listing"]))
    buyer = market["buyer"]
    assert cli.main(["market", "verify", str(listing_path), "--dest", buyer]) == 0
    assert "verified: lift" in capsys.readouterr().out
    assert cli.main(["market", "install", str(listing_path), "--accept-licence", "CC-BY-4.0", "--dest", buyer]) == 0
    assert "status=review" in capsys.readouterr().out
    other = _store(tmp_path, "other")
    assert cli.main(["market", "verify", str(listing_path), "--dest", other]) == 1
    assert cli.main(["market", "list", "--dest", market["fleet_a"]]) == 0
    assert "retry-flaky-network" in capsys.readouterr().out
    card_path = tmp_path / "card.json"
    assert cli.main(["market", "identity", "--role", "publisher", "--id", "o", "--organization", "o",
                     "--dest", other]) == 0
    card_path.write_text(capsys.readouterr().out)
    assert cli.main(["market", "trust", str(card_path), "--role", "publisher", "--dest", buyer]) == 0


def test_a_signed_but_schema_invalid_lesson_is_refused(market, tmp_path):
    from commontrace import market_listing, origin

    pub = market["ids"]["pub"]
    record = json.loads(json.dumps(market["listing"]["record"]))
    del record["lesson"]["frontmatter"]["applies_when"]
    record["lesson"]["frontmatter"]["importance"] = 9
    record["lesson_sha256"] = market_listing.lesson_digest(record["lesson"])
    resigned = origin.bind(record, pub)  # a real publisher signing a malformed lesson
    report = marketplace.verify(resigned, marketplace.load_trust(market["buyer"]))
    assert not report["ok"]
    assert any("lacks applies_when" in p for p in report["problems"])
    assert any("importance" in p for p in report["problems"])


def _activate(root, slug):
    path = lesson_io.lesson_path(root, slug)
    fm, body = frontmatter.read(path)
    lesson_io.write_lesson(path, {**fm, "status": "active"}, body, root=root, actor="op", reason="approved")


def test_settlement_pays_fees_and_shares_only_locally_proven_lift(market, capsys):
    from commontrace import holdout_io, market_settlement

    licence = {"id": "COMMERCIAL-1", "terms": "Per-install fee plus 20% of proven value.",
               "price": {"amount_usd": 12, "per": "install"}, "outcome_share": 0.2}
    listing = marketplace.publish(market["fleet_a"], "retry-flaky-network", market["certificate"], licence)
    buyer = market["buyer"]
    row = marketplace.install(buyer, listing, accept_licence="COMMERCIAL-1")
    period = row["installed_at"][:7]
    before = market_settlement.settle(buyer, period, value_per_occasion=10.0)
    assert [line["kind"] for line in before["lines"]] == ["fee-install"]
    assert before["lines"][0]["amount_usd"] == 12.0
    assert "not active" in before["refused"][0]["reason"]  # still in review: no share

    _activate(buyer, row["slug"])
    holdout_io.configure(buyer, rate=0.5, salt="buyer")
    for i in range(600):
        occasion = f"b{i}"
        withheld = holdout_io.assign_and_log(buyer, [row["slug"]], occasion_id=occasion, rate=0.5, salt="buyer",
                                             durable=False)
        holdout_io.record_outcome(buyer, occasion, (i % 10) < (3 if withheld else 8), durable=False)
    statement = market_settlement.settle(buyer, period, value_per_occasion=10.0, commit=True)
    share = next(line for line in statement["lines"] if line["kind"] == "outcome-share")
    evidence = share["evidence"]
    assert evidence["verdict"] == "HELPS" and evidence["ci_low"] > 0
    assert share["amount_usd"] == round(evidence["basis_usd"] * 0.2, 2) > 0
    assert statement["totals_usd"][listing["organization"]] == round(12.0 + share["amount_usd"], 2)
    with pytest.raises(market_settlement.SettlementError, match="already settled"):
        market_settlement.settle(buyer, period, value_per_occasion=10.0)

    year, month = map(int, period.split("-"))
    later = f"{year + (month == 12)}-{month % 12 + 1:02d}"
    nothing_new = market_settlement.settle(buyer, later, value_per_occasion=10.0, commit=True)
    assert nothing_new["lines"] == [] and nothing_new["previous_digest"] == statement["digest"]
    assert cli_main_settle(buyer, later) == 1  # the CLI refuses a settled period too


def cli_main_settle(root, period):
    from commontrace import cli

    return cli.main(["market", "settle", "--period", period, "--value-per-occasion", "10", "--dest", root])


def test_licences_reject_unknown_terms_and_oversized_shares():
    from commontrace import market_listing

    with pytest.raises(market_listing.MarketError, match="unknown licence terms"):
        market_listing._licence({"id": "X", "terms": "t", "royalty": 1})
    with pytest.raises(market_listing.MarketError, match="outcome_share"):
        market_listing._licence({"id": "X", "terms": "t", "outcome_share": 0.9})
