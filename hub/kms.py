"""Bring your own key: the Hub's at-rest key held by the customer's KMS, not by the Hub."""
from __future__ import annotations

import base64

from hub.encryption import EncryptionError

CONTEXT = {"purpose": "commontrace-hub-encryption-key"}


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
