"""Attack-focused tests for UniMem capability attenuation and request binding."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from unimem import capabilities as cap
from unimem import frontier_protocol as fp

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)
ROOT_KEY, A_KEY, B_KEY, C_KEY = b"r" * 32, b"a" * 32, b"b" * 32, b"c" * 32
KEYS = {"agent-a": A_KEY, "agent-b": B_KEY, "agent-c": C_KEY}


def policy(**updates):
    return {"namespaces": ["facts", "lessons"], "operations": ["read", "write", "forget", "export"],
            "classes": ["general", "procedure"], "purposes": ["support", "assembly"],
            "labels": ["project:a", "agent:a"], "delegates": ["agent-b", "agent-c"],
            "ceiling": 2, "audience": "agent-a", "expires_at": (NOW + timedelta(hours=1)).isoformat(), **updates}


def grant():
    return cap.grant(policy(), issuer="operator", tenant="tenant-a", nonce="grant", now=NOW.isoformat(),
                     algorithm="hmac-sha256", key=ROOT_KEY)


def verify(token, **updates):
    return cap.verify(token, issuer="operator", tenant="tenant-a", algorithm="hmac-sha256", key=ROOT_KEY,
                      holder_key=KEYS.__getitem__, now=NOW.isoformat(), **updates)


@pytest.mark.parametrize("update", [
    {"namespaces": ["facts", "lessons", "private"]}, {"ceiling": 3},
    {"expires_at": (NOW + timedelta(days=1)).isoformat()}, {"audience": "intruder"},
    {"classes": ["general", "procedure", "secret"]}, {"purposes": ["support", "advertising"]},
    {"labels": ["project:b"]}, {"operations": ["admin"]},
])
def test_widening_is_rejected(update):
    with pytest.raises((PermissionError, ValueError)):
        cap.attenuate(grant(), policy(**update), key=A_KEY)


def test_valid_attenuation_cannot_strip_intermediate_holder():
    root = grant()
    intermediate = cap.attenuate(root, policy(audience="agent-b"), key=A_KEY)
    descendant = cap.attenuate(intermediate, policy(audience="agent-c", delegates=[]), key=B_KEY)
    assert verify(descendant)["caveats"]["audience"] == "agent-c"
    # The leaf knows every portable prefix MAC. It lacks agent A's private key.
    forged = cap.attenuate(root, policy(audience="agent-c", delegates=[]), key=C_KEY)
    with pytest.raises(PermissionError, match="signature"):
        verify(forged)
    for chain_id in verify(descendant)["chain_ids"]:
        with pytest.raises(PermissionError, match="revoked"):
            verify(descendant, revoked=[chain_id])


def test_signature_parent_tenant_algorithm_expiry_and_unknown_fields_fail_closed():
    root = grant()
    child = cap.attenuate(root, policy(audience="agent-b", namespaces=["facts"]), key=A_KEY)
    for mutation in ("parent", "tenant", "algorithm", "signature", "unknown", "version"):
        forged = deepcopy(child)
        if mutation == "parent":
            forged["links"][1]["record"]["parent"] = "0" * 64
        elif mutation == "tenant":
            forged["links"][0]["record"]["tenant"] = "tenant-b"
        elif mutation == "algorithm":
            forged["algorithm"] = "ed25519"
        elif mutation == "signature":
            forged["links"][0]["signature"] = "0" * 64
        elif mutation == "unknown":
            forged["links"][1]["record"]["caveats"]["allow_admin"] = True
        else:
            forged["version"] = True
        with pytest.raises((PermissionError, ValueError)):
            verify(forged)
    with pytest.raises(PermissionError, match="expired"):
        cap.verify(root, issuer="operator", tenant="tenant-a", algorithm="hmac-sha256", key=ROOT_KEY,
                   holder_key=KEYS.__getitem__, now=(NOW + timedelta(hours=1)).isoformat())


def test_proof_binds_body_purpose_token_tenant_audience_and_freshness():
    token = grant()
    digest = cap.request_digest("POST", "/v1/memory/search?limit=2", b'{"q":"wrist"}', purpose="support")
    proof = cap.prove(token, key=A_KEY, algorithm="hmac-sha256", audience="agent-a", tenant="tenant-a",
                      request_digest=digest, nonce="fresh", now=NOW.isoformat())
    checked = verify(token)
    assert cap.verify_proof(token, checked, proof, key=A_KEY, algorithm="hmac-sha256", request_digest=digest,
                            now=NOW.isoformat()) == "fresh"
    for wrong in (cap.request_digest("POST", "/v1/memory/search?limit=3", b'{"q":"wrist"}', purpose="support"),
                  cap.request_digest("POST", "/v1/memory/search?limit=2", b'{"q":"other"}', purpose="support"),
                  cap.request_digest("POST", "/v1/memory/search?limit=2", b'{"q":"wrist"}', purpose="assembly")):
        with pytest.raises(PermissionError):
            cap.verify_proof(token, checked, proof, key=A_KEY, algorithm="hmac-sha256", request_digest=wrong,
                             now=NOW.isoformat())
    with pytest.raises(PermissionError):
        cap.verify_proof(token, checked, proof, key=A_KEY, algorithm="hmac-sha256", request_digest=digest,
                         now=(NOW + timedelta(seconds=30)).isoformat())


def test_ed25519_chain_uses_pinned_public_keys():
    ed = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    def public(key):
        return ed.Ed25519PrivateKey.from_private_bytes(key).public_key().public_bytes_raw()
    token = cap.grant(policy(), issuer="foreign", tenant="tenant-a", nonce="root", now=NOW.isoformat(),
                      algorithm="ed25519", key=ROOT_KEY)
    token = cap.attenuate(token, policy(audience="agent-b", namespaces=["facts"]), key=A_KEY)
    checked = cap.verify(token, issuer="foreign", tenant="tenant-a", algorithm="ed25519", key=public(ROOT_KEY),
                         holder_key=lambda name: public(KEYS[name]), now=NOW.isoformat())
    assert checked["caveats"]["namespaces"] == ["facts"]
    with pytest.raises(PermissionError):
        cap.verify(token, issuer="foreign", tenant="tenant-a", algorithm="ed25519", key=public(C_KEY),
                   holder_key=lambda name: public(KEYS[name]), now=NOW.isoformat())


def test_chain_size_and_label_validation_are_bounded():
    for value in ("*", " support", "support\n"):
        with pytest.raises(ValueError):
            cap.grant(policy(purposes=[value]), issuer="operator", tenant="tenant-a", nonce="root",
                      now=NOW.isoformat(), algorithm="hmac-sha256", key=ROOT_KEY)
    token = grant()
    for _ in range(15):
        token = cap.attenuate(token, policy(), key=A_KEY)
    assert len(verify(token)["chain_ids"]) == 16
    with pytest.raises(ValueError, match="depth"):
        cap.attenuate(token, policy(), key=A_KEY)
    assert fp.digest(token) == verify(token)["chain_digest"]
