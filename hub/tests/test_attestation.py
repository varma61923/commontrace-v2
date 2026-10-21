"""Hub attestation tests use real Postgres isolation and explicit test-only quotes."""
from __future__ import annotations

from dataclasses import replace

import pytest

from unimem import attested_keys as keys
from unimem import frontier_protocol as fp
from hub import attestation, kms
from hub.tests.test_rest import _app, _client, _key, _org_with_key

pytestmark = pytest.mark.asyncio

BUILD = {"backend": "simulated", "claims": {"test_build": "hub-integration-only"},
         "description": "Explicit simulator fixture; no hardware claim"}


class FakeTenantKeyProvider:
    def __init__(self):
        self.calls = []

    def unwrap(self, tenant):
        self.calls.append(tenant)
        return b"t"*32


async def test_kms_provider_binds_ciphertext_to_exact_authenticated_tenant_context():
    import base64

    class KMS:
        def __init__(self):
            self.calls = []

        def decrypt(self, **request):
            self.calls.append(request)
            return {"Plaintext": b"k"*32}

    client = KMS()
    tenants = {"org-a": {"key_id": "alias/a", "ciphertext": base64.b64encode(b"wrapped").decode()}}
    provider = kms.TenantKeyProvider(tenants, client=client)
    assert provider.unwrap("org-a") == b"k"*32
    assert client.calls[0]["EncryptionContext"] == {
        "purpose": "unimem-attested-tenant-key-v1", "tenant": "org-a"}
    with pytest.raises(RuntimeError, match="not provisioned"):
        provider.unwrap("org-b")

    class InvalidKMS(KMS):
        def decrypt(self, **request):
            return {"Plaintext": b"short"}

    with pytest.raises(RuntimeError, match="invalid attested key"):
        kms.TenantKeyProvider(provider.tenants, client=InvalidKMS()).unwrap("org-a")


def simulated_services(app):
    verifier = keys.SimulatedVerifier(b"hub-only-integration-test-key-32bytes!!")
    provider = FakeTenantKeyProvider()
    attestation.install_services(app, provider, {"simulated": verifier}, allow_simulated=True)
    return provider, verifier


@pytest.fixture
def worker():
    pytest.importorskip("cryptography", reason="attested envelopes require the optional attest extra")
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private = X25519PrivateKey.generate()
    return private, private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def decrypt(envelope, value, private):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    shared = private.exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(envelope["ephemeral_key"])))
    wrapping = HKDF(algorithm=hashes.SHA256(), length=32, salt=bytes.fromhex(fp.digest(value)),
                    info=b"unimem-attested-key-v1").derive(shared)
    return AESGCM(wrapping).decrypt(bytes.fromhex(envelope["nonce"]), bytes.fromhex(envelope["ciphertext"]),
                                  fp.wire(value))


async def test_measurement_challenge_encrypted_release_replay_and_revocation(session_factory, config, worker):
    org, api_key = await _org_with_key(session_factory)
    cfg = replace(config, ledger_signing_key="test-attestation-ledger-signing-key-over-32-bytes")
    app = _app(session_factory, cfg)
    provider, verifier = simulated_services(app)
    async with _client(app) as client:
        assert (await client.post("/v1/attestation/measurements-list", json={})).status_code == 401
        headers = _key(api_key)
        registered = await client.post("/v1/attestation/measurements-register", headers=headers,
                                       json={"profile": BUILD})
        assert registered.status_code == 200, registered.text
        enrollment = registered.json()
        measurement = fp.digest(keys.profile(BUILD))
        assert enrollment["measurement"] == measurement
        from hub.attestation import enrolled
        from hub.db import session_scope

        async with session_scope(session_factory, org_id=org) as session:
            await enrolled(session, org, measurement, {"secret": cfg.ledger_signing_key,
                                                       "algorithm": cfg.transparency_algorithm})
        issued = await client.post("/v1/attestation/challenge", headers=headers, json={
            "measurement": measurement, "worker_key": worker[1], "operation": "recall",
            "payload_digest": "cd"*32})
        assert issued.status_code == 200, issued.text
        request = issued.json()["request"]
        quote = verifier.quote(request, BUILD)
        released = await client.post("/v1/attestation/key-release", headers=headers,
                                     json={"request": request, "evidence": quote})
        assert released.status_code == 200, released.text
        assert decrypt(released.json()["envelope"], request, worker[0]) == b"t"*32
        assert released.json()["attestation"]["simulated"] is True
        assert provider.calls == [org]
        assert (await client.post("/v1/attestation/key-release", headers=headers,
                                  json={"request": request, "evidence": quote})).status_code == 403
        revoked = await client.post("/v1/attestation/measurements-register", headers=headers,
                                    json={"profile": BUILD, "revoke": True})
        assert revoked.status_code == 200 and revoked.json()["revoked"]
        assert (await client.post("/v1/attestation/challenge", headers=headers, json={
            "measurement": measurement, "worker_key": worker[1], "operation": "recall",
            "payload_digest": "cd"*32})).status_code == 403


async def test_tenant_rls_registration_and_challenges_cannot_cross_organizations(session_factory, config, worker):
    org, key_a = await _org_with_key(session_factory, name="attest-a")
    other, key_b = await _org_with_key(session_factory, name="attest-b")
    app = _app(session_factory, replace(config, ledger_signing_key="test-attestation-ledger-signing-key-over-32-bytes"))
    _, verifier = simulated_services(app)
    async with _client(app) as client:
        headers_a, headers_b = _key(key_a), _key(key_b)
        await client.post("/v1/attestation/measurements-register", headers=headers_a, json={"profile": BUILD})
        measurement = fp.digest(keys.profile(BUILD))
        value = (await client.post("/v1/attestation/challenge", headers=headers_a, json={
            "measurement": measurement, "worker_key": worker[1], "operation": "recall",
            "payload_digest": "cd"*32})).json()["request"]
        quote = verifier.quote(value, BUILD)
        forged = {**value, "tenant": other, "operation": "join", "participants": sorted([org, other])}
        response = await client.post("/v1/attestation/key-release", headers=headers_b,
                                     json={"request": forged, "evidence": quote})
        assert response.status_code == 403
        assert (await client.post("/v1/attestation/measurements-list", headers=headers_b, json={})
                ).json()["measurements"] == []
        assert (await client.post("/v1/attestation/measurements-list", headers=headers_a, json={})
                ).json()["measurements"][0]["measurement"] == measurement
        from hub import scopes

        _, reader = await _org_with_key(session_factory, name="attest-reader", scopes=[scopes.SCOPE_READ])
        denied = await client.post("/v1/attestation/measurements-list", headers=_key(reader), json={})
        assert denied.status_code == 403
