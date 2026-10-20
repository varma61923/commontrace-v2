from __future__ import annotations

import pytest

from commontrace import cleanroom, holdout_io, overlap, valuation
from commontrace import frontier_protocol as fp


def test_sybils_share_one_reward_and_do_not_inflate_total():
    sig = overlap.minhash("check clearance before moving the robot arm")
    offer = {"id": "a", "seller": "established", "minhash": sig, "stake_cents": 10_000}
    baseline = cleanroom.royalties([offer], {"a": .5}, 1000)
    copies = [{**offer, "id": str(i), "seller": str(i), "stake_cents": 1} for i in range(30)]
    attacked = cleanroom.royalties([offer, *copies], {"a": .5, **{str(i): .5 for i in range(30)}}, 1000)
    assert attacked["paid_cents"] <= baseline["paid_cents"] == 1000
    assert len(attacked["clusters"]) == 1
    # Thirty unstaked copies cannot divert the originator's royalty.
    assert attacked["payouts"]["established"] >= 990
    assert cleanroom.overlap_price(1000, sig, sig) == 0


def test_hamilton_royalties_distribute_every_cent_deterministically():
    signatures = [[value] * 16 for value in range(3)]
    offers = [{"id": identity, "seller": identity, "minhash": signature, "stake_cents": 10}
              for identity, signature in zip(("a", "b", "c"), signatures)]
    equal_clusters = cleanroom.royalties(offers, {identity: .5 for identity in ("a", "b", "c")}, 100)
    assert equal_clusters["paid_cents"] == 100
    assert equal_clusters["payouts"] == {"a": 34, "b": 33, "c": 33}
    assert cleanroom.royalties(offers[::-1], {identity: .5 for identity in ("a", "b", "c")},
                               100) == equal_clusters

    shared = [{**offer, "minhash": signatures[0]} for offer in offers]
    equal_sellers = cleanroom.royalties(shared, {identity: .5 for identity in ("a", "b", "c")}, 100)
    assert equal_sellers["paid_cents"] == 100
    assert equal_sellers["payouts"] == {"a": 34, "b": 33, "c": 33}


def test_dp_accountant_refuses_overspend_and_budget_reset(tmp_path):
    root = str(tmp_path)
    first = cleanroom.private_effect(root, "buyer", "query1", [.2] * 100, epsilon=.6, total_budget=1)
    assert cleanroom.private_effect(root, "buyer", "query1", [.8] * 100, epsilon=.6) == first
    with pytest.raises(PermissionError, match="budget exhausted"):
        cleanroom.private_effect(root, "buyer", "query2", [.2] * 100, epsilon=.6, total_budget=10)
    assert cleanroom.private_effect(root, "buyer", "query3", [.2] * 100, epsilon=.4)["spent"] == 1


def test_sealed_trial_does_not_return_content_and_escrow_requires_attested_evidence(tmp_path):
    seller, buyer = str(tmp_path / "seller"), str(tmp_path / "buyer")
    offer = cleanroom.listing(seller, seller="vendor", handle="sandbox:sealed", content_digest="a" * 64,
                             minhash=overlap.minhash("private lesson"), stake_cents=100)
    holdout_io.configure(buyer, rate=.5, salt="buyer")
    class Runner:
        def run(self, handle, occasion, *, injected):
            return injected
        def attest(self, handle):
            return {"enclave": "mock", "handle": handle}
    result = cleanroom.trial(buyer, offer, Runner(), "task", verify_offer=lambda x: fp.verify(seller, x),
                            verify_attestation=lambda x: x["enclave"] == "mock")
    assert not result["content_exposed"] and "private lesson" not in str(result)
    with pytest.raises(PermissionError):
        cleanroom.settle(buyer, "escrow", offer, valuation.certificate(buyer, {}), amount_cents=100,
                         verify_offer=lambda x: fp.verify(seller, x))
    with pytest.raises(PermissionError):
        cleanroom.trial(buyer, offer, Runner(), "task2", verify_offer=lambda x: False,
                        verify_attestation=lambda x: True)


def test_stripe_disabled_without_explicit_configuration():
    with pytest.raises(PermissionError):
        cleanroom.StripeConnect(None).pay({"status": "released", "amount_cents": 10, "escrow": "e"}, "acct_1")


def test_escrow_positive_evidence_releases_once_for_exact_content(tmp_path):
    seller, buyer = str(tmp_path / 'seller'), str(tmp_path / 'buyer')
    offer = cleanroom.listing(seller, seller='vendor', handle='opaque', content_digest='a' * 64,
                             minhash=overlap.minhash('lesson'), stake_cents=100)
    identity = offer['record']['id']
    holdout_io.configure(buyer, rate=.5, salt='escrow')
    for i in range(400):
        held = holdout_io.assign_and_log(buyer, [identity], occasion_id=str(i), rate=.5, salt='escrow',
                                        revisions={identity: 'a' * 64})
        holdout_io.record_outcome(buyer, str(i), identity not in held)
    cert = valuation.certify_logged(buyer, [identity], propensity_floor=.5)
    def verifier(x):
        return fp.verify(seller, x)
    result = cleanroom.settle(buyer, 'escrow', offer, cert, amount_cents=100, verify_offer=verifier)
    assert result['status'] == 'released'
    assert cleanroom.settle(buyer, 'escrow', offer, cert, amount_cents=100, verify_offer=verifier) == result
    tampered = {**offer, 'record': {**offer['record'], 'content_digest': 'b' * 64}}
    with pytest.raises(PermissionError):
        cleanroom.settle(buyer, 'other', tampered, cert, amount_cents=100, verify_offer=verifier)


def test_configured_payments_reject_fabricated_escrow(tmp_path):
    with pytest.raises(ValueError, match='trusted local'):
        cleanroom.StripeConnect(None, enabled=True)
    payments = cleanroom.StripeConnect(None, enabled=True, root=str(tmp_path))
    with pytest.raises(PermissionError, match='authenticated'):
        payments.pay({'status': 'released', 'escrow': 'fake', 'amount_cents': 100}, 'acct_1')
