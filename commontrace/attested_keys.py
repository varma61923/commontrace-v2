"""Log-gated, nonce-bound tenant key release; simulation is never a hardware claim.

Hardware adapters verify signed appraisal results from operator-pinned services.
They do not implement AMD/Intel certificate-chain verification in Python.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from commontrace import _jsonl
from commontrace import frontier_protocol as fp

HEX = re.compile(r"[0-9a-f]{64}\Z")
BACKENDS = frozenset({"simulated", "amd-sev-snp", "intel-tdx", "nvidia-gpu"})


class Verifier(Protocol):
    backend: str
    simulated: bool

    def verify(self, evidence: Mapping[str, Any], request: Mapping[str, Any],
               profile: Mapping[str, Any]) -> None: ...


def digest(value: Any) -> str:
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError("UniMem attestation needs a canonical SHA-256/32-byte hex value")
    return value


def profile(value: Mapping[str, Any]) -> dict[str, Any]:
    """A measurement ID commits the complete audited appraisal policy."""
    if set(value) != {"backend", "claims", "description"} or value["backend"] not in BACKENDS:
        raise ValueError("UniMem unsupported attestation measurement profile")
    fp.text(value["description"], "audited build description", 1024)
    claims = value["claims"]
    if not isinstance(claims, dict) or not claims or len(claims) > 64 or len(fp.wire(claims)) > 32_768:
        raise ValueError("UniMem measurements require bounded, nonempty pinned build claims")
    for key in claims:
        fp.text(key, "attested claim", 128)
    backend = value["backend"]
    if backend == "amd-sev-snp":
        required = {"x-ms-sevsnpvm-launchmeasurement", "x-ms-sevsnpvm-snpfw-svn",
                    "x-ms-sevsnpvm-tee-svn", "x-ms-sevsnpvm-microcode-svn",
                    "x-ms-sevsnpvm-bootloader-svn"}
    elif backend == "intel-tdx":
        required = {"tdx_mrtd", "tdx_rtmr0", "tdx_rtmr1", "tdx_rtmr2", "tdx_rtmr3",
                    "tdx_tee_tcb_svn", "tdx_seamsvn", "attester_tcb_status"}
    elif backend == "nvidia-gpu":
        required = {"cpu_profile", "gpu_claims"}
        if set(claims) != required or profile(claims["cpu_profile"])["backend"] not in {
                "amd-sev-snp", "intel-tdx"}:
            raise ValueError("UniMem GPU release also requires an attested CPU join/worker build")
        gpu = claims["gpu_claims"]
        if not isinstance(gpu, dict) or not gpu or len(gpu) > 32:
            raise ValueError("UniMem GPU measurement needs explicit expected firmware/driver claims")
    else:
        required = {"test_build"}
    if not required <= set(claims):
        raise ValueError("UniMem measurement must pin all required boot/runtime registers")
    if backend in {"amd-sev-snp", "intel-tdx"}:
        for field in required:
            if backend == "amd-sev-snp" and field != "x-ms-sevsnpvm-launchmeasurement":
                fp.count(claims[field], 0, 2**32-1, field)
            elif backend == "intel-tdx" and field == "tdx_seamsvn":
                fp.count(claims[field], 0, 2**16-1, field)
            elif backend == "intel-tdx" and field == "attester_tcb_status":
                if claims[field] != "UpToDate":
                    raise ValueError("UniMem TDX release requires the current UpToDate TCB appraisal")
            elif not isinstance(claims[field], str) or not re.fullmatch(
                    r"[0-9a-f]{32}" if field == "tdx_tee_tcb_svn" else r"[0-9a-f]{96}", claims[field]):
                raise ValueError("UniMem CPU measurements require canonical SHA-384 register values")
    required_gpu = {"x-nvidia-device-type", "x-nvidia-gpu-architecture"}
    if backend == "nvidia-gpu" and not required_gpu <= set(claims["gpu_claims"]):
        raise ValueError("UniMem GPU measurement must pin device type and architecture")
    return cast(dict[str, Any], json.loads(fp.wire(dict(value))))  # detach caller-owned dictionaries


def request(tenant: str, measurement: str, worker_key: str, *, operation: str = "recall",
            payload_digest: str, participants: Sequence[str] = (), expires_seconds: int = 60,
            nonce: str | None = None) -> dict[str, Any]:
    fp.text(tenant, "tenant", 128)
    if operation not in {"recall", "sealed-trial", "join"}:
        raise ValueError("UniMem unsupported attested key operation")
    fp.count(expires_seconds, 1, 120, "attestation challenge lifetime")
    members = sorted(set(participants) or {tenant})
    fp.count(len(members), 1, 8, "join parties")
    for member in members:
        fp.text(member, "join tenant", 128)
    if tenant not in members or (operation == "join") != (len(members) > 1):
        raise ValueError("UniMem join requests require all parties including their own tenant")
    issued = fp.now()
    return {"schema": "unimem.attestation-request.v1", "tenant": tenant,
        "measurement": digest(measurement), "worker_key": digest(worker_key), "operation": operation,
        "payload_digest": digest(payload_digest), "participants": members,
        "nonce": digest(nonce) if nonce is not None else secrets.token_hex(32), "issued_at": issued,
        "expires_at": (datetime.fromisoformat(issued)+timedelta(seconds=expires_seconds)).isoformat()}


def validate_request(value: Mapping[str, Any]) -> None:
    fields = {"schema", "tenant", "measurement", "worker_key", "operation", "payload_digest", "participants",
              "nonce", "issued_at", "expires_at"}
    if set(value) != fields or value["schema"] != "unimem.attestation-request.v1":
        raise ValueError("UniMem invalid attestation request schema")
    for key in ("measurement", "worker_key", "payload_digest", "nonce"):
        digest(value[key])
    fp.text(value["tenant"], "tenant", 128)
    members = value["participants"]
    if (not isinstance(members, list) or not 1 <= len(members) <= 8
            or any(not isinstance(member, str) or not member or len(member) > 128 for member in members)
            or sorted(set(members)) != members or value["tenant"] not in members
            or value["operation"] not in {"recall", "sealed-trial", "join"}
            or (value["operation"] == "join") != (len(members) > 1)):
        raise ValueError("UniMem invalid attested operation or party set")
    start = datetime.fromisoformat(fp.moment(value["issued_at"]))
    end = datetime.fromisoformat(fp.moment(value["expires_at"]))
    now = datetime.fromisoformat(fp.now())
    if not start <= now < end or not 0 < (end-start).total_seconds() <= 120:
        raise PermissionError("UniMem attestation request is expired or outside its freshness bound")


class SimulatedVerifier:
    """Explicit test-key HMAC appraisal: provides no hardware isolation."""
    backend = "simulated"
    simulated = True

    def __init__(self, test_key: bytes):
        if not isinstance(test_key, bytes) or len(test_key) < 32:
            raise ValueError("UniMem simulation requires an explicit test key of at least 32 bytes")
        self._key = test_key

    def quote(self, value: Mapping[str, Any], build: Mapping[str, Any]) -> dict[str, Any]:
        validate_request(value)
        selected = profile(build)
        if selected["backend"] != self.backend or fp.digest(selected) != value["measurement"]:
            raise PermissionError("UniMem simulation quote build differs from the enrolled measurement")
        body = {"backend": self.backend, "simulated": True, "hardware_isolation": False,
                "request_hash": fp.digest(value), "measurement": value["measurement"]}
        return {"record": body, "mac": hmac.new(self._key, fp.wire(body), hashlib.sha256).hexdigest()}

    def verify(self, evidence: Mapping[str, Any], value: Mapping[str, Any], build: Mapping[str, Any]) -> None:
        expected = self.quote(value, build)
        if not hmac.compare_digest(fp.wire(dict(evidence)), fp.wire(expected)):
            raise PermissionError("UniMem simulated test quote is invalid or binds a different request")


class AzureVerifier:
    """Verify MAA JWTs against operator-pinned public keys; never follow JWT URLs.

    The deployment must submit hardware report-data containing the request hash
    followed by 32 zero bytes. Azure HCL configurations that reserve report-data
    for a different binding cannot use this profile unchanged.
    """
    simulated = False

    def __init__(self, backend: str, *, issuer: str, keys: Mapping[str, Any], policy_hash: str):
        if backend not in {"amd-sev-snp", "intel-tdx"} or not issuer.startswith("https://"):
            raise ValueError("UniMem MAA needs a pinned HTTPS issuer and supported CPU backend")
        if not keys or len(keys) > 32:
            raise ValueError("UniMem MAA requires bounded operator-pinned verifier keys")
        self.backend, self.issuer, self.keys, self.policy_hash = backend, issuer, dict(keys), fp.text(policy_hash)

    def verify(self, evidence: Mapping[str, Any], value: Mapping[str, Any], build: Mapping[str, Any]) -> None:
        try:
            import jwt
        except ImportError:
            raise RuntimeError("UniMem MAA verification requires the optional attest extra") from None
        if set(evidence) != {"token"} or not isinstance(evidence["token"], str) or len(evidence["token"]) > 131_072:
            raise ValueError("UniMem expected one bounded signed MAA token")
        try:
            header = jwt.get_unverified_header(evidence["token"])
            if (header.get("alg") != "RS256" or header.get("kid") not in self.keys
                    or header.get("crit")):
                raise PermissionError("UniMem unsupported MAA signing key or algorithm")
            claims = jwt.decode(evidence["token"], self.keys[header["kid"]], algorithms=["RS256"],
                issuer=self.issuer, options={"require": ["exp", "iat", "nbf", "iss"], "verify_aud": False})
        except jwt.PyJWTError:
            raise PermissionError("UniMem MAA token failed cryptographic or freshness verification") from None
        now = datetime.fromisoformat(fp.now()).timestamp()
        if (any(type(claims.get(field)) is not int for field in ("iat", "nbf", "exp"))
                or not claims["iat"] <= now < claims["exp"] or now-claims["iat"] > 120):
            raise PermissionError("UniMem MAA appraisal is stale or has invalid timestamps")
        expected_type = "sevsnpvm" if self.backend == "amd-sev-snp" else "tdxvm"
        debug = (claims.get("x-ms-sevsnpvm-is-debuggable") is False if self.backend == "amd-sev-snp" else
                 claims.get("tdx_td_attributes_debug") is False and claims.get("dbgstat") == "disabled"
                 and claims.get("attester_tcb_status") == "UpToDate")
        report = "x-ms-sevsnpvm-reportdata" if self.backend == "amd-sev-snp" else "tdx_report_data"
        vm_config = claims.get("x-ms-runtime", {}).get("vm-configuration", {})
        secure_boot = (vm_config.get("secure-boot") is True and vm_config.get("tpm-enabled") is True
                       if self.backend == "amd-sev-snp" else True)
        if (claims.get("x-ms-attestation-type") != expected_type or not debug or not secure_boot
                or claims.get("x-ms-compliance-status") != "azure-compliant-cvm"
                or claims.get("x-ms-policy-hash") != self.policy_hash
                or claims.get(report) != fp.digest(value)+"00"*32
                or any(claims.get(key) != wanted for key, wanted in build["claims"].items())):
            raise PermissionError("UniMem MAA build, policy, debug state or request binding was rejected")


class NvidiaVerifier:
    """Actual NVAT verify-token plus a separately verified CPU build.

    GPU firmware evidence alone does not measure the CPU worker/join executable.
    The operator pins the native SDK binary and the required authenticated GPU
    claims; both attestations bind this exact request digest.
    """
    backend = "nvidia-gpu"
    simulated = False

    def __init__(self, program: str, binary_digest: str, cpu: AzureVerifier):
        self.program, self.binary_digest, self.cpu = Path(program), digest(binary_digest), cpu
        self._check_binary()

    def _check_binary(self) -> None:
        path = self.program
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or path.stat().st_mode & 0o022 or not os.access(path, os.X_OK)):
            raise PermissionError("UniMem NVAT requires a pinned regular executable owned by the deployment")
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b""):
                hasher.update(chunk)
        observed = hasher.hexdigest()
        if observed != self.binary_digest:
            raise PermissionError("UniMem NVAT verifier binary differs from its operator pin")

    def verify(self, evidence: Mapping[str, Any], value: Mapping[str, Any], build: Mapping[str, Any]) -> None:
        import subprocess

        if set(evidence) != {"cpu", "gpu"}:
            raise ValueError("UniMem GPU release requires both CPU and GPU evidence")
        self.cpu.verify(evidence["cpu"], value, build["claims"]["cpu_profile"])
        self._check_binary()
        raw = fp.wire(evidence["gpu"])
        if len(raw) > 256*1024:
            raise ValueError("UniMem GPU evidence exceeds 256 KiB")
        env = {key: val for key, val in os.environ.items() if key not in {
            "NV_ATTESTATION_SERVICE_KEY", "NVAT_NRAS_URL"}}
        try:
            result = subprocess.run([str(self.program), "verify-token", "--token-file", "-", "--nras-url",
                "https://nras.attestation.nvidia.com", "--nonce", fp.digest(value), "--format", "json"],
                input=raw, capture_output=True, timeout=30, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("UniMem native NVIDIA attestation verifier is unavailable") from None
        if result.returncode or len(result.stdout) > 512*1024:
            raise PermissionError("UniMem NVIDIA SDK rejected the attestation")
        decoded = json.loads(result.stdout)
        claims = decoded.get("claims")
        if (decoded.get("result_code") != 0 or not isinstance(claims, list) or not 1 <= len(claims) <= 16
                or any(not isinstance(row, dict) or row.get("secboot") is not True
                       or row.get("x-nvidia-device-type") != "gpu"
                       or row.get("dbgstat") != "disabled"
                       or any(row.get(key) != wanted for key, wanted in build["claims"]["gpu_claims"].items())
                       for row in claims)):
            raise PermissionError("UniMem authenticated GPU claims do not satisfy the enrolled measurement")


def verify_quote(value: Mapping[str, Any], evidence: Mapping[str, Any], build: Mapping[str, Any],
                 verifier: Verifier, *, allow_simulated: bool = False) -> dict[str, Any]:
    validate_request(value)
    selected = profile(build)
    if selected["backend"] != verifier.backend or fp.digest(selected) != value["measurement"]:
        raise PermissionError("UniMem quote verifier and registered measurement differ")
    if verifier.simulated and not allow_simulated:
        raise PermissionError("UniMem simulated evidence is disabled for production key release")
    verifier.verify(evidence, value, selected)
    return {"request_hash": fp.digest(value), "measurement": value["measurement"],
            "backend": verifier.backend, "simulated": verifier.simulated, "hardware_isolation": not verifier.simulated}


def seal_key(key: bytes, value: Mapping[str, Any]) -> dict[str, Any]:
    """Encrypt only to the worker public key authenticated by the quote."""
    validate_request(value)
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("UniMem attested release requires exactly one 256-bit tenant data key")
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError:
        raise RuntimeError("UniMem encrypted worker key release requires the optional attest extra") from None
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(value["worker_key"])))
    binding = bytes.fromhex(fp.digest(value))
    wrapping = HKDF(algorithm=hashes.SHA256(), length=32, salt=binding,
                    info=b"unimem-attested-key-v1").derive(shared)
    nonce = secrets.token_bytes(12)
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return {"schema": "unimem.worker-key.v1", "request_hash": binding.hex(),
            "ephemeral_key": ephemeral.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex(),
            "nonce": nonce.hex(), "ciphertext": AESGCM(wrapping).encrypt(nonce, key, fp.wire(dict(value))).hex()}


def assert_join(requests: Sequence[Mapping[str, Any]]) -> None:
    fp.count(len(requests), 2, 8, "join parties")
    for item in requests:
        validate_request(item)
    first = requests[0]
    if (any(item["operation"] != "join" or any(item[field] != first[field] for field in (
            "measurement", "worker_key", "payload_digest", "participants")) for item in requests)
            or sorted(item["tenant"] for item in requests) != first["participants"]):
        raise PermissionError("UniMem every join party must release only to the same attested binary and worker")


def verify_node(receipt: Mapping[str, Any], measurement: str, principal: Any, *,
                checkpoint: Mapping[str, Any], revoked: Sequence[str] = ()) -> bool:
    """Client appraisal against an independently pinned public log checkpoint.

    The caller maintains the latest trusted revocation set/checkpoint and obtains
    an inclusion proof at that checkpoint. Membership alone cannot establish
    that an unobserved later revocation never happened.
    """
    from commontrace import origin, transparency_log

    try:
        if measurement in revoked or principal.algorithm != "ed25519":
            return False
        statement, head = receipt["statement"], receipt["head"]
        if (dict(head) != dict(checkpoint) or not origin.verify(head, {principal.id: principal})
                or not origin.verify(statement, {principal.id: principal})):
            return False
        body = statement["record"]["statement"]
        if (body.get("event") != "attested-measurement" or body.get("revoked") is not False
                or body.get("measurement") != digest(measurement)
                or fp.digest(profile(body["profile"])) != measurement):
            return False
        return transparency_log.verify_inclusion(transparency_log.leaf_hash(statement), receipt["leaf"],
            head["record"]["tree_size"], receipt["proof"], head["record"]["root_hash"])
    except (KeyError, ValueError, TypeError):
        return False


def register(root: str, build: Mapping[str, Any], *, revoke: bool = False) -> dict[str, Any]:
    from commontrace import nextwave_store, transparency_log

    nextwave_store.operator()
    if type(revoke) is not bool:
        raise ValueError("UniMem measurement revocation requires a boolean")
    selected = profile(build)
    identity = fp.digest(selected)
    with _jsonl.locked(fp.filename(root, "attested-measurements.lock")):
        rows = {row["measurement"]: row for row in fp.journal(root, "attested-measurements.jsonl")}
        prior = rows.get(identity)
        if prior and (prior["revoked"] or not revoke):
            return prior
        body = {"measurement": identity, "profile": selected, "revoked": bool(revoke), "at": fp.now()}
        anchor = transparency_log.append(root, "attested-measurement", body)
        return dict(fp.append(root, "attested-measurements.jsonl", {**body, "anchor": anchor})["record"])


def measurements(root: str) -> list[dict[str, Any]]:
    from commontrace import nextwave_store

    nextwave_store.operator()
    return list({row["measurement"]: row for row in fp.journal(root, "attested-measurements.jsonl")}.values())


def admitted(root: str, measurement: str) -> dict[str, Any]:
    from commontrace import transparency_log

    selected = next((row for row in measurements(root) if row["measurement"] == digest(measurement)), None)
    if selected is None or selected["revoked"] or not transparency_log.verify(root, selected["anchor"]):
        raise PermissionError("UniMem build measurement is unregistered, revoked or missing its log receipt")
    expected = {key: selected[key] for key in ("measurement", "profile", "revoked", "at")}
    record = selected["anchor"].get("statement", {}).get("record", {})
    if record.get("event") != "attested-measurement" or record.get("statement") != expected:
        raise PermissionError("UniMem build measurement does not match its log inclusion receipt")
    return dict(selected["profile"])


def issue(root: str, tenant: str, measurement: str, worker_key: str, **options: Any) -> dict[str, Any]:
    from commontrace import nextwave_store

    nextwave_store.operator()
    with _jsonl.locked(fp.filename(root, "attested-measurements.lock")):
        admitted(root, measurement)
        value = request(tenant, measurement, worker_key, **options)
        fp.append(root, "attested-challenges.jsonl", {"request": value})
        return value


def release(root: str, value: Mapping[str, Any], evidence: Mapping[str, Any], verifier: Verifier,
            unwrap: Callable[[str], bytes], *, allow_simulated: bool = False) -> dict[str, Any]:
    from commontrace import nextwave_store, transparency_log

    nextwave_store.operator()
    # Serialize revocation, challenge use and release. Consume before unwrapping:
    # a process crash cannot replay the challenge to obtain a second envelope.
    with _jsonl.locked(fp.filename(root, "attested-measurements.lock")):
        selected = admitted(root, value["measurement"])
        if not any(row["request"] == value for row in fp.journal(root, "attested-challenges.jsonl")):
            raise PermissionError("UniMem an operator-issued fresh challenge is required")
        nonce = digest(value["nonce"])
        if any(row["nonce"] == nonce for row in fp.journal(root, "attested-key-releases.jsonl")):
            raise PermissionError("UniMem attestation challenge nonce was already consumed")
        result = verify_quote(value, evidence, selected, verifier, allow_simulated=allow_simulated)
        fp.append(root, "attested-key-releases.jsonl", {"nonce": nonce, **result, "tenant": value["tenant"]})
        sealed = seal_key(unwrap(value["tenant"]), value)
        anchor = transparency_log.append(root, "attested-key-release", {**result, "envelope_digest": fp.digest(sealed)})
        return {"envelope": sealed, "attestation": result, "anchor": anchor}


def accept_result(root: str, value: Mapping[str, Any], evidence: Mapping[str, Any], verifier: Verifier,
                  *, allow_simulated: bool = False) -> dict[str, Any]:
    """Verify and consume a fresh result challenge without unwrapping any key."""
    from commontrace import nextwave_store, transparency_log

    nextwave_store.operator()
    if value.get("operation") != "sealed-trial":
        raise PermissionError("UniMem sealed result appraisal requires a dedicated result challenge")
    with _jsonl.locked(fp.filename(root, "attested-measurements.lock")):
        selected = admitted(root, value["measurement"])
        if not any(row["request"] == value for row in fp.journal(root, "attested-challenges.jsonl")):
            raise PermissionError("UniMem result challenge was not issued by this store")
        nonce = digest(value["nonce"])
        if any(row["nonce"] == nonce for row in fp.journal(root, "attested-key-releases.jsonl")):
            raise PermissionError("UniMem result challenge nonce was already consumed")
        result = verify_quote(value, evidence, selected, verifier, allow_simulated=allow_simulated)
        fp.append(root, "attested-key-releases.jsonl", {"nonce": nonce, **result, "tenant": value["tenant"]})
        anchor = transparency_log.append(root, "attested-sealed-result", {
            **result, "result_digest": value["payload_digest"]})
        return {"request": dict(value), "quote": dict(evidence), "attestation": result, "anchor": anchor}
