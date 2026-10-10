import json

import pytest

from benchmarks.signed_results import sign, verify


def test_public_signature_tampering_and_pinned_trust():
    crypto = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    key = crypto.Ed25519PrivateKey.generate()
    report = {'benchmark': 'fixture', 'public_aggregate_only': True, 'metrics': {'accuracy': None},
              'limitations': ['No answer-quality measurement']}
    envelope = sign(report, key)
    trusted = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    assert verify(envelope, trusted_key=trusted) == report
    modified = json.loads(json.dumps(envelope))
    modified['report']['metrics']['accuracy'] = .9
    with pytest.raises(ValueError):
        verify(modified, trusted_key=trusted)
    with pytest.raises(ValueError):
        verify(envelope, trusted_key='00' * 32)
    with pytest.raises(ValueError):
        sign({'public_aggregate_only': False, 'prompt': 'private data'}, key)
