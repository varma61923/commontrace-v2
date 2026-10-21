"""Protocol tests use explicit simulated keys, never fabricated hardware evidence."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from unimem import attested_keys as keys
from unimem import cleanroom, holdout_io, memory_authority, overlap
from unimem import frontier_protocol as fp

BUILD = {"backend": "simulated", "claims": {"test_build": "test-only-join-binary-v1"},
         "description": "Simulated test build; no hardware isolation"}


def verifier():
    return keys.SimulatedVerifier(b"explicit-test-key-not-for-production-"*2)


def challenge(root, *, worker="ab"*32, tenant="tenant-a", **options):
    enrolled = keys.register(root, BUILD)
    return keys.issue(root, tenant, enrolled["measurement"], worker, payload_digest="cd"*32, **options)


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


@pytest.fixture
def worker():
    pytest.importorskip("cryptography", reason="encrypted worker release requires the optional attest extra")
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private = X25519PrivateKey.generate()
    return private, private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def test_unregistered_measurements_and_simulated_production_evidence_are_denied(tmp_path):
    root = str(tmp_path)
    with pytest.raises(PermissionError, match="unregistered"):
        keys.issue(root, "tenant-a", "ab"*32, "cd"*32, payload_digest="ef"*32)
    value = challenge(root)
    quote = verifier().quote(value, BUILD)
    called = []
    with pytest.raises(PermissionError, match="simulated"):
        keys.release(root, value, quote, verifier(), lambda tenant: called.append(tenant))
    assert called == []


@pytest.mark.parametrize("field,replacement", [("tenant", "other"), ("worker_key", "ef"*32),
    ("payload_digest", "ef"*32), ("nonce", "ef"*32), ("measurement", "ef"*32)])
def test_quote_binds_every_security_relevant_request_field(tmp_path, field, replacement):
    value = challenge(str(tmp_path))
    quote = verifier().quote(value, BUILD)
    changed = {**value, field: replacement}
    with pytest.raises((PermissionError, ValueError)):
        keys.verify_quote(changed, quote, BUILD, verifier(), allow_simulated=True)


def test_nonce_is_consumed_once_and_only_the_attested_worker_can_unwrap(tmp_path, worker):
    private, public = worker
    root = str(tmp_path)
    value = challenge(root, worker=public)
    quote = verifier().quote(value, BUILD)
    called = []
    def unwrap(tenant):
        called.append(tenant)
        return b"k"*32
    released = keys.release(root, value, quote, verifier(), unwrap, allow_simulated=True)
    assert decrypt(released["envelope"], value, private) == b"k"*32
    assert released["attestation"]["simulated"] and not released["attestation"]["hardware_isolation"]
    assert (b"k"*32).hex() not in json.dumps(released)
    with pytest.raises(PermissionError, match="consumed"):
        keys.release(root, value, quote, verifier(), unwrap, allow_simulated=True)
    assert called == ["tenant-a"]


def test_revocation_is_monotone_and_blocks_previously_issued_quotes(tmp_path):
    root = str(tmp_path)
    value = challenge(root)
    quote = verifier().quote(value, BUILD)
    assert keys.register(root, BUILD, revoke=True)["revoked"]
    assert keys.register(root, BUILD)["revoked"]
    with pytest.raises(PermissionError, match="revoked"):
        keys.release(root, value, quote, verifier(), lambda tenant: b"k"*32, allow_simulated=True)
    with memory_authority.writer("agent", "agent"):
        with pytest.raises(PermissionError):
            keys.register(root, BUILD)


def test_expired_quote_and_nonce_not_issued_by_this_store_are_denied(tmp_path, monkeypatch):
    root = str(tmp_path)
    value = challenge(root)
    quote = verifier().quote(value, BUILD)
    future = datetime.fromisoformat(value["expires_at"])+timedelta(seconds=1)
    monkeypatch.setattr(fp, "now", lambda: future.isoformat())
    with pytest.raises(PermissionError, match="expired"):
        keys.release(root, value, quote, verifier(), lambda tenant: b"k"*32, allow_simulated=True)


def test_concurrent_release_and_kms_failure_cannot_replay_an_unwrap(tmp_path, worker):
    root = str(tmp_path)
    value = challenge(root, worker=worker[1])
    quote = verifier().quote(value, BUILD)
    called = []
    def unwrap(tenant):
        called.append(tenant)
        raise RuntimeError("simulated KMS outage")
    def release():
        try:
            keys.release(root, value, quote, verifier(), unwrap, allow_simulated=True)
        except (PermissionError, RuntimeError) as exc:
            return type(exc).__name__
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(lambda _: release(), range(2)))
    assert results == ["PermissionError", "RuntimeError"]
    assert called == ["tenant-a"]


def test_cross_org_join_requires_same_binary_worker_query_and_complete_parties():
    measurement = fp.digest(BUILD)
    first = keys.request("a", measurement, "ab"*32, operation="join", payload_digest="cd"*32,
                         participants=["a", "b"])
    second = keys.request("b", measurement, "ab"*32, operation="join", payload_digest="cd"*32,
                          participants=["a", "b"])
    keys.assert_join([first, second])
    for field in ("measurement", "worker_key", "payload_digest"):
        with pytest.raises(PermissionError):
            keys.assert_join([first, {**second, field: "ef"*32}])
    with pytest.raises(PermissionError):
        keys.assert_join([first, first])


def test_simulated_sealed_trial_emits_a_quote_bound_to_its_actual_result(tmp_path):
    root = str(tmp_path)
    simulator = verifier()
    enrolled = keys.register(root, BUILD)
    initial = challenge(root)
    initial_quote = simulator.quote(initial, BUILD)
    offer = cleanroom.listing(root, seller="test-vendor", handle="sealed:test", content_digest="ab"*32,
                             minhash=overlap.minhash("private seller-side lesson"), stake_cents=100)
    holdout_io.configure(root, rate=.5, salt="sealed-test")
    class Runner:
        def attest(self, handle):
            return initial_quote
        def run(self, handle, occasion, *, injected):
            return injected
    def preflight(quote):
        return bool(keys.verify_quote(initial, quote, BUILD, simulator, allow_simulated=True))
    result = cleanroom.attested_trial(root, offer, Runner(), "task", verify_offer=lambda x: fp.verify(root, x),
        verify_attestation=preflight, tenant="tenant-a", measurement=enrolled["measurement"], worker_key="ab"*32,
        result_quote=lambda request: simulator.quote(request, BUILD), verifier=simulator, allow_simulated=True)
    assert result["result_digest"] == fp.digest(result["result"]) == result["request"]["payload_digest"]
    assert result["attestation"]["simulated"]
    assert keys.verify_quote(result["request"], result["quote"], BUILD, simulator, allow_simulated=True)
    altered = {**result["request"], "payload_digest": "cd"*32}
    with pytest.raises(PermissionError):
        keys.verify_quote(altered, result["quote"], BUILD, simulator, allow_simulated=True)


@pytest.mark.parametrize("backend", ["amd-sev-snp", "intel-tdx"])
def test_real_jwt_verifier_checks_signatures_report_binding_and_measured_registers(backend):
    jwt = pytest.importorskip("jwt", reason="MAA appraisal verification requires the optional attest extra")
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

    private = generate_private_key(public_exponent=65537, key_size=2048)
    registers = ({"x-ms-sevsnpvm-launchmeasurement": "ab"*48,
                  "x-ms-sevsnpvm-snpfw-svn": 7, "x-ms-sevsnpvm-tee-svn": 3,
                  "x-ms-sevsnpvm-microcode-svn": 9, "x-ms-sevsnpvm-bootloader-svn": 2}
                 if backend == "amd-sev-snp" else
                 {**{name: "ab"*48 for name in ["tdx_mrtd", "tdx_rtmr0", "tdx_rtmr1", "tdx_rtmr2", "tdx_rtmr3"]},
                  "tdx_tee_tcb_svn": "ab"*16, "tdx_seamsvn": 3,
                  "attester_tcb_status": "UpToDate"})
    build = keys.profile({"backend": backend, "description": "Unit fixture for real JWT validation", "claims": registers})
    value = keys.request("tenant", fp.digest(build), "cd"*32, payload_digest="ef"*32)
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {**registers, "iat": now, "nbf": now, "exp": now+60, "iss": "https://fixture.attest.azure.net",
              "x-ms-policy-hash": "pinned-policy", "x-ms-attestation-type": "sevsnpvm" if backend ==
              "amd-sev-snp" else "tdxvm", "x-ms-compliance-status": "azure-compliant-cvm"}
    if backend == "amd-sev-snp":
        claims.update({"x-ms-sevsnpvm-is-debuggable": False, "x-ms-sevsnpvm-reportdata": fp.digest(value)+"00"*32,
                       "x-ms-runtime": {"vm-configuration": {"secure-boot": True, "tpm-enabled": True}}})
    else:
        claims.update({"tdx_td_attributes_debug": False, "dbgstat": "disabled", "attester_tcb_status": "UpToDate",
                       "tdx_report_data": fp.digest(value)+"00"*32})
    adapter = keys.AzureVerifier(backend, issuer=claims["iss"], keys={"fixture": private.public_key()},
                                 policy_hash="pinned-policy")
    def signed(body):
        return {"token": jwt.encode(body, private, algorithm="RS256", headers={"kid": "fixture"})}
    assert keys.verify_quote(value, signed(claims), build, adapter)["simulated"] is False
    for field, replacement in [(next(iter(registers)), "cd"*48), ("x-ms-policy-hash", "other"),
                                ("iat", now-130), ("exp", now-1)]:
        altered = {**claims, field: replacement}
        with pytest.raises(PermissionError):
            keys.verify_quote(value, signed(altered), build, adapter)
    other = generate_private_key(public_exponent=65537, key_size=2048)
    wrong = {"token": jwt.encode(claims, other, algorithm="RS256", headers={"kid": "fixture"})}
    with pytest.raises(PermissionError):
        keys.verify_quote(value, wrong, build, adapter)
    changed = deepcopy(value)
    changed["worker_key"] = "ef"*32
    with pytest.raises(PermissionError):
        keys.verify_quote(changed, signed(claims), build, adapter)
