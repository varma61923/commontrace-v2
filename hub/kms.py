"""Bring your own key: the Hub's at-rest key held by the customer's KMS, not by the Hub.

`HUB_ENCRYPTION_KEY` (hub/encryption.py) protects the few at-rest fields the Hub encrypts itself (webhook URLs,
connector secrets). Held in an environment variable it is only as safe as that variable. With BYOK the
environment holds a WRAPPED key, a blob only the customer's KMS key can open, and the Hub unwraps it at start-up
and keeps the plaintext in memory only:

    HUB_ENCRYPTION_KEY_KMS_WRAPPED=<base64 blob>        instead of HUB_ENCRYPTION_KEY
    HUB_ENCRYPTION_KEY_PREVIOUS_KMS_WRAPPED=<a,b,...>   instead of HUB_ENCRYPTION_KEY_PREVIOUS (rotation)
    HUB_KMS_KEY_ID=<key arn>   HUB_KMS_REGION=<region>

What that buys: the customer controls and audits the key (every unwrap is a CloudTrail event carrying the
encryption context below), and revoking the Hub's grant makes the Hub unable to start or decrypt, which is the
customer's off switch. Create the blob with `python -m hub.manage wrap-encryption-key <key arn> <region>`.

What it does not cover, said plainly: trace and lesson TEXT. Postgres computes full-text search over those columns,
so encrypting them in the application would silently break search (hub/encryption.py explains). They are protected
at the storage layer instead, with a customer-managed key: see deploy/terraform (`kms_key_arn` on AWS, `kms_key_name`
on GCP). Together those are the Hub's BYOK story; this module is the application-layer half.

AWS only for now, through `boto3` (not a Hub requirement: install it in the image that uses this). An unwrap that
the KMS refuses stops the Hub with a message that says so, never falling back to running unencrypted.
"""
from __future__ import annotations

import base64

from hub.encryption import EncryptionError

#: Bound into every wrap and unwrap, so a blob made for something else cannot be opened as this key, and so each
#: use shows up in the customer's audit log with a recognisable purpose.
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
        request["KeyId"] = key_id          # pin the key: a blob wrapped under another key is refused
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
    """A new random data key, returned only in its wrapped form (base64). The plaintext is never returned or
    printed: it exists in this process for the length of one call."""
    kms = client or _client(region)
    try:
        out = kms.generate_data_key(KeyId=key_id, KeySpec="AES_256", EncryptionContext=CONTEXT)
    except Exception as exc:  # noqa: BLE001
        raise EncryptionError(f"the KMS refused to generate a data key ({type(exc).__name__}): {exc}") from None
    return base64.b64encode(out["CiphertextBlob"]).decode("ascii")


def resolve(env: dict, secret) -> tuple[str, str]:
    """(current, previous) as HubConfig takes them, from either plain or wrapped settings, never both.
    `secret(name)` reads a setting the way the rest of config does (including `{NAME}_FILE`)."""
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
