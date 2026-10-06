"""Security and RPC boundaries: metadata, encryption and RPC batches."""
import asyncio

import pytest

from commontrace import memory_guard

abuse = pytest.importorskip("hub.abuse")
run_batch = pytest.importorskip("hub.batches").run_batch
HubConfig = pytest.importorskip("hub.config").HubConfig
encryption = pytest.importorskip("hub.encryption")
EncryptionError = encryption.EncryptionError
EnvelopeCipher = encryption.EnvelopeCipher
generate_key = encryption.generate_key


def test_metadata_secrets_are_scanned_in_nested_tags_and_cycles():
    secret = 'ghp_' + 'a' * 40
    fields = {'tags': [{'note': secret}]}
    fields['cycle'] = fields
    report = memory_guard.scan_fields(fields)
    assert report.should_block
    assert any(f.field == 'tags[0].note' for f in report.findings)
    assert abuse.suspicion_reason(fields, HubConfig(database_url='postgresql+asyncpg://unused'))


def test_excessive_metadata_fails_closed_before_expanding():
    with pytest.raises(ValueError, match='metadata nodes'):
        memory_guard.scan_fields({'tags': ['safe'] * 100_001})


@pytest.mark.parametrize('value', ['ctenc:v1:!!!', 'ctenc:v1:aA==', 'ctenc:v1:☃'])
def test_malformed_encryption_envelope_has_uniform_safe_error(value):
    pytest.importorskip('cryptography.hazmat.primitives.ciphers.aead')
    cipher = EnvelopeCipher.from_config(generate_key(), '')
    with pytest.raises(EncryptionError, match='envelope'):
        cipher.decrypt(value)


def test_batch_validates_all_shapes_before_any_side_effect():
    calls = []

    async def handler(id):
        calls.append(id)
        return {'id': id}

    result = asyncio.run(run_batch(handler, ['first', 7], field='id'))
    assert result['error'] == 'invalid_batch'
    assert calls == []
    result = asyncio.run(run_batch(handler, [{'id': 'first'}, {'unknown': 'value'}]))
    assert result['error'] == 'invalid_batch'
    assert calls == []


def test_batch_keeps_order_and_independent_refusals():
    calls = []

    async def handler(id):
        calls.append(id)
        return {'error': 'not_found'} if id == 'missing' else {'id': id}

    result = asyncio.run(run_batch(handler, ['first', 'missing', 'last'], field='id'))
    assert result == {'results': [{'id': 'first'}, {'error': 'not_found'}, {'id': 'last'}]}
    assert calls == ['first', 'missing', 'last']


@pytest.mark.parametrize('items', [[], ['x'] * 26, 'x'])
def test_batch_limits_are_enforced(items):
    async def handler(id):
        pytest.fail('invalid batch cannot execute')
    assert asyncio.run(run_batch(handler, items, field='id'))['error'] == 'invalid_batch'
