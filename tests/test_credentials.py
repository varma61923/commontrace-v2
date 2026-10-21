"""Real Ed25519 SD-JWT disclosure, key binding and W3C status-list bits."""
from __future__ import annotations

import base64
import gzip
import json

import pytest

from unimem import credentials as vc

pytest.importorskip("cryptography", reason="credentials use the optional security extra")

ISSUER = "https://issuer.example.test"
STATUS = ISSUER+"/status/1"
NOW = 1800000000
PRIVATE = bytes(range(32))
HOLDER = bytes(range(1, 33))
KEYS = {(ISSUER, "key-1"): vc.public_key(PRIVATE)}


def issue(**changes):
    arguments = {"issuer": ISSUER, "kid": "key-1", "signing_key": PRIVATE,
        "holder_public": vc.public_key(HOLDER), "credential_id": "urn:uuid:credential-1",
        "status_url": STATUS, "status_index": 7, "issued_at": NOW, "expires_at": NOW+3600}
    return vc.issue({"statement": "The operator prefers quiet notifications.", "subjectId": "private-person-54321",
                     "modelDigest": "sha256:"+"a"*64, "confidence": .9}, **{**arguments, **changes})


def status(indices=()):
    return vc.status_list(indices, issuer=ISSUER, kid="key-1", signing_key=PRIVATE,
                          status_url=STATUS, issued_at=NOW, expires_at=NOW+300)


def verify(presentation, **changes):
    arguments = {"issuer_keys": KEYS, "audience": "https://verifier.example.test", "nonce": "fresh-verifier-nonce",
        "now": NOW+1, "is_revoked": lambda entry: vc.revoked(status(), entry, issuer_keys=KEYS, issuer=ISSUER, now=NOW+1)}
    return vc.verify(presentation, **{**arguments, **changes})


def present(issued, claims=("statement",), **changes):
    arguments = {"holder_key": HOLDER, "audience": "https://verifier.example.test",
                 "nonce": "fresh-verifier-nonce", "issued_at": NOW+1}
    return vc.present(issued, claims, **{**arguments, **changes})


def test_disclosed_values_verify_and_undisclosed_values_are_absent_from_presentation():
    issued = issue()
    presentation = present(issued)
    result = verify(presentation)
    assert result["credentialSubject"] == {"statement": "The operator prefers quiet notifications."}
    assert result["holder_bound"] and result["@context"][0] == vc.VC_CONTEXT
    decoded_parts = []
    for component in presentation.split("~"):
        for part in component.split(".")[:2]:
            decoded_parts.append(base64.urlsafe_b64decode(part+"="*(-len(part) % 4)))
    assert all(b"private-person-54321" not in part and b"sha256:"+b"a"*64 not in part for part in decoded_parts)
    assert "private-person-54321" not in json.dumps(result)
    # Fresh salts prevent equal facts from sharing disclosure commitments.
    assert issue().split("~")[1] != issued.split("~")[1]
    assert verify(present(issued, ())) ["credentialSubject"] == {}


def test_wrong_issuer_algorithm_key_or_modified_claim_is_rejected():
    presentation = present(issue())
    with pytest.raises(PermissionError, match="pinned"):
        verify(presentation, issuer_keys={})
    with pytest.raises(PermissionError, match="signature"):
        verify(presentation, issuer_keys={(ISSUER, "key-1"): vc.public_key(bytes([9])*32)})
    pieces = presentation.split("~")
    header, payload, _, _ = vc._parts(pieces[0])
    payload["credentialStatus"]["statusListIndex"] = "0"
    old = pieces[0].split(".")
    pieces[0] = old[0]+"."+vc._b64(json.dumps(payload).encode())+"."+old[2]
    with pytest.raises(PermissionError, match="signature"):
        verify("~".join(pieces))
    header["alg"] = "none"
    pieces[0] = vc._b64(json.dumps(header).encode())+"."+old[1]+"."+old[2]
    with pytest.raises(PermissionError, match="algorithm"):
        verify("~".join(pieces))


def test_holder_binding_requires_correct_key_fresh_audience_nonce_and_exact_disclosures():
    issued = issue()
    presentation = present(issued)
    with pytest.raises(PermissionError, match="holder"):
        present(issued, holder_key=bytes([8])*32)
    for changes in ({"audience": "another-verifier"}, {"nonce": "another-nonce"}, {"now": NOW+62}):
        with pytest.raises(PermissionError, match="binding"):
            verify(presentation, **changes)
    with pytest.raises(PermissionError, match="binding"):
        verify(issued)
    pieces = presentation.split("~")
    with pytest.raises(PermissionError, match="binding"):
        verify(pieces[0]+"~"+pieces[-1])
    # A second disclosure appended after holder signing cannot be substituted.
    alternate = issue().split("~")[1]
    with pytest.raises(PermissionError, match="disclosure"):
        verify(pieces[0]+"~"+alternate+"~"+pieces[-1])
    with pytest.raises(PermissionError, match="disclosure"):
        verify(pieces[0]+"~"+pieces[1]+"~"+pieces[1]+"~"+pieces[-1])


def test_expiry_revocation_unknown_status_and_wrong_status_list_are_denied():
    presentation = present(issue())
    with pytest.raises(PermissionError, match="time"):
        verify(presentation, now=NOW+3600)
    with pytest.raises(PermissionError, match="revoked"):
        verify(presentation, is_revoked=lambda entry: vc.revoked(status([7]), entry,
               issuer_keys=KEYS, issuer=ISSUER, now=NOW+1))
    for unknown in (None, "false", 0, True):
        with pytest.raises(PermissionError, match="status"):
            verify(presentation, is_revoked=lambda entry: unknown)
    entry = verify(presentation)["credentialStatus"]
    with pytest.raises(PermissionError, match="time"):
        vc.revoked(status(), entry, issuer_keys=KEYS, issuer=ISSUER, now=NOW+300)
    with pytest.raises(PermissionError, match="match"):
        vc.revoked(status(), {**entry, "statusListCredential": ISSUER+"/another"},
                   issuer_keys=KEYS, issuer=ISSUER, now=NOW+1)


def test_w3c_status_list_uses_multibase_gzip_minimum_size_and_leftmost_index():
    token = status([0, 7, 8, vc.STATUS_BITS-1])
    _, payload, _, _ = vc._parts(token)
    encoded = payload["credentialSubject"]["encodedList"]
    assert encoded.startswith("u")
    compressed = base64.urlsafe_b64decode(encoded[1:]+"="*(-len(encoded[1:]) % 4))
    bits = gzip.decompress(compressed)
    assert len(bits) == 16384 and bits[0] == 0x81 and bits[1] == 0x80 and bits[-1] == 0x01
    for index in [0, 7, 8, vc.STATUS_BITS-1, 1, 6, 9]:
        entry = {"id": STATUS+"#"+str(index), "statusListIndex": str(index), "statusListCredential": STATUS}
        assert vc.revoked(token, entry, issuer_keys=KEYS, issuer=ISSUER, now=NOW+1) is (index in [0, 7, 8, vc.STATUS_BITS-1])


def test_disclosure_and_status_bounds_reject_ambiguous_or_excessive_inputs():
    with pytest.raises(ValueError, match="field"):
        vc.issue({"_sd": "nested"}, issuer=ISSUER, kid="key-1", signing_key=PRIVATE,
                 holder_public=vc.public_key(HOLDER), credential_id="urn:test:1", status_url=STATUS,
                 status_index=0, issued_at=NOW, expires_at=NOW+60)
    with pytest.raises(ValueError, match="distinct"):
        present(issue(), ("statement", "statement"))
    with pytest.raises(ValueError, match="not in"):
        present(issue(), ("unknown",))
    with pytest.raises(ValueError, match="duplicate"):
        vc._json(b'{"iss":"trusted","iss":"other"}')
    with pytest.raises(ValueError):
        issue(status_index=vc.STATUS_BITS)


def test_rfc9901_published_disclosure_digest_uses_encoded_ascii_not_decoded_json():
    import hashlib

    disclosure = ('WyJfMjZiYzRMVC1hYzZxMktJNmNCVzVlcyIsICJmYW1pbHlfbmFtZSIsICJNw7ZiaXVzIl0')
    assert vc._json(vc._unb64(disclosure))[1:] == ["family_name", "Möbius"]
    assert vc._b64(hashlib.sha256(disclosure.encode("ascii")).digest()) == 'X9yH0Ajrdm1Oij4tWso9UzzKJvPoDxwmuEcO3XAdRC0'


def test_signed_payload_with_foreign_jsonld_context_or_public_private_claim_is_not_this_profile():
    for mutate in (lambda payload: payload["@context"].append("https://untrusted.example.test/context"),
                   lambda payload: payload.update(subjectId="secret accidentally made public")):
        issued = issue()
        parts = issued.split("~")
        _, payload, _, _ = vc._parts(parts[0])
        mutate(payload)
        parts[0] = vc._sign(payload, PRIVATE, typ="vc+sd-jwt", kid="key-1")
        with pytest.raises(PermissionError, match="profile"):
            verify(present("~".join(parts)))
