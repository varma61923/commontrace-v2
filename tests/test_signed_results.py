import json

import pytest

from benchmarks.signed_results import sign, verify


def test_public_signature_tampering_and_pinned_trust():
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = crypto.Ed25519PrivateKey.generate()
    report = {
        "benchmark": "fixture",
        "public_aggregate_only": True,
        "metrics": {"accuracy": None},
        "limitations": ["No answer-quality measurement"],
    }
    envelope = sign(report, key)
    trusted = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    assert verify(envelope, trusted_key=trusted) == report
    modified = json.loads(json.dumps(envelope))
    modified["report"]["metrics"]["accuracy"] = 0.9
    with pytest.raises(ValueError):
        verify(modified, trusted_key=trusted)
    with pytest.raises(ValueError):
        verify(envelope, trusted_key="00" * 32)
    with pytest.raises(ValueError):
        sign({"public_aggregate_only": False, "prompt": "private data"}, key)


def test_hash_bound_public_artifacts_reject_tampering_and_escape(tmp_path):
    import hashlib

    from benchmarks.signed_results import verify_artifacts

    path = tmp_path / "aggregate.json"
    path.write_text('{"count": 1}')
    report = {"files_sha256": {"aggregate.json": hashlib.sha256(path.read_bytes()).hexdigest()}}
    verify_artifacts(report, str(tmp_path))
    path.write_text('{"count": 2}')
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_artifacts(report, str(tmp_path))
    with pytest.raises(ValueError, match="configured root"):
        verify_artifacts({"files_sha256": {"../outside": "0" * 64}}, str(tmp_path))
