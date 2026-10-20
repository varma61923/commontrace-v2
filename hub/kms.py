"""Bring your own key: the Hub's at-rest key held by the customer's KMS, not by the Hub."""
from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any, Protocol

from hub.encryption import EncryptionError

CONTEXT = {"purpose": "commontrace-hub-encryption-key"}


class SubjectKeyService(Protocol):
    """Deployment-owned subject-key destruction boundary, separate from backups.

    A production KMS adapter must attest destruction across replicas and refuse
    old unwraps. The existing global envelope key alone cannot erase backups.
    """

    def destroy(self, subject: str) -> dict: ...
    def reference(self, subject: str) -> str: ...


def reference_subject_vault(store_root: str, directory: str):
    """Filesystem reference for isolated deployments; production KMS may replace it."""
    from commontrace.certified_erasure import SubjectVault

    return SubjectVault(store_root, directory)


def _client(region: str | None):
    try:
        import boto3
    except ImportError:
        raise EncryptionError(
            "HUB_ENCRYPTION_KEY_KMS_WRAPPED is set but boto3 is not installed; install it in the image "
            "that runs the Hub") from None
    return boto3.client("kms", region_name=region or None)


def unwrap(wrapped: str, *, key_id: str | None = None, region: str | None = None, client=None) -> str:
    """The data key as urlsafe base64, as `HUB_ENCRYPTION_KEY` takes it."""
    try:
        blob = base64.b64decode(wrapped.strip().encode("ascii"), validate=True)
    except Exception:
        raise EncryptionError("a KMS-wrapped key is not valid base64") from None
    kms = client or _client(region)
    request = {"CiphertextBlob": blob, "EncryptionContext": CONTEXT}
    if key_id:
        request["KeyId"] = key_id
    try:
        plaintext = kms.decrypt(**request)["Plaintext"]
    except Exception as exc:  # noqa: BLE001 - every KMS failure means the same thing here
        raise EncryptionError(
            f"the KMS refused to unwrap the encryption key ({type(exc).__name__}). If the customer revoked "
            "access, that is working as intended: the Hub cannot decrypt until it is granted again.") from None
    if len(plaintext) != 32:
        raise EncryptionError(f"the unwrapped key is {len(plaintext)} bytes, expected 32")
    return base64.urlsafe_b64encode(plaintext).decode("ascii")


def generate_wrapped(key_id: str, *, region: str | None = None, client=None) -> str:
    kms = client or _client(region)
    try:
        out = kms.generate_data_key(KeyId=key_id, KeySpec="AES_256", EncryptionContext=CONTEXT)
    except Exception as exc:  # noqa: BLE001
        raise EncryptionError(f"the KMS refused to generate a data key ({type(exc).__name__}): {exc}") from None
    return base64.b64encode(out["CiphertextBlob"]).decode("ascii")


class TenantKeyProvider:
    """Deployment-provisioned AWS KMS envelopes with mandatory tenant context.

    These keys are distinct from the Hub's global at-rest envelope key. The
    request cannot select a ciphertext, KMS key ID, encryption context or region.
    Provision each envelope using this exact context and an appropriate KMS IAM
    policy before enabling attested release.
    """
    def __init__(self, tenants: Mapping[str, Mapping[str, str]], *, client: Any = None, region: str | None = None):
        self.tenants, self.client, self.region = dict(tenants), client, region

    def unwrap(self, tenant: str) -> bytes:
        entry = self.tenants.get(tenant)
        if entry is None or set(entry) != {"ciphertext", "key_id"}:
            raise RuntimeError("UniMem tenant attested key is not provisioned")
        try:
            blob = base64.b64decode(entry["ciphertext"], validate=True)
            result = (self.client or _client(self.region)).decrypt(CiphertextBlob=blob, KeyId=entry["key_id"],
                EncryptionContext={"purpose": "commontrace-attested-tenant-key-v1", "tenant": tenant})
            key = result["Plaintext"]
        except Exception:
            raise RuntimeError("UniMem tenant KMS refused the attested data-key unwrap") from None
        if not isinstance(key, bytes) or len(key) != 32:
            raise RuntimeError("UniMem tenant KMS returned an invalid attested key")
        return key


def resolve(env: dict, secret) -> tuple[str, str]:
    plain, wrapped = secret("HUB_ENCRYPTION_KEY"), secret("HUB_ENCRYPTION_KEY_KMS_WRAPPED")
    plain_prev, wrapped_prev = secret("HUB_ENCRYPTION_KEY_PREVIOUS"), secret("HUB_ENCRYPTION_KEY_PREVIOUS_KMS_WRAPPED")
    if plain and wrapped:
        raise EncryptionError("set HUB_ENCRYPTION_KEY or HUB_ENCRYPTION_KEY_KMS_WRAPPED, not both")
    if plain_prev and wrapped_prev:
        raise EncryptionError("set HUB_ENCRYPTION_KEY_PREVIOUS or HUB_ENCRYPTION_KEY_PREVIOUS_KMS_WRAPPED, not both")
    if not wrapped and not wrapped_prev:
        return plain, plain_prev
    key_id, region = env.get("HUB_KMS_KEY_ID", "").strip() or None, env.get("HUB_KMS_REGION", "").strip() or None
    current = unwrap(wrapped, key_id=key_id, region=region) if wrapped else plain
    previous = ",".join(unwrap(item, key_id=key_id, region=region)
                        for item in wrapped_prev.split(",") if item.strip()) if wrapped_prev else plain_prev
    return current, previous
