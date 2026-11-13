"""Envelope encryption for the handful of at-rest fields where it is safe."""

from __future__ import annotations

import base64
import binascii
import json
import secrets as _secrets
from dataclasses import dataclass

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ModuleNotFoundError:  # pragma: no cover - exercised when cryptography isn't installed
    AESGCM = None  # type: ignore[assignment,misc]

_PREFIX = "ctenc:v1:"
_BOUND_PREFIX = "ctenc:v2:"
_KEY_BYTES = 32
_NONCE_BYTES = 12


class EncryptionError(ValueError): ...


def field_aad(org_id: str, table: str, record_id: str, column: str) -> bytes:
    """Unambiguous authentication context; none of these identifiers is secret."""
    return json.dumps(["unimem-field-v2", str(org_id), table, str(record_id), column], separators=(",", ":")).encode()


def generate_key() -> str:
    return base64.urlsafe_b64encode(_secrets.token_bytes(_KEY_BYTES)).decode("ascii")


def _decode_key(value: str, *, source: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise EncryptionError(f"{source} is not valid urlsafe-base64: {exc}") from None
    if len(raw) != _KEY_BYTES:
        raise EncryptionError(
            f"{source} must decode to exactly {_KEY_BYTES} bytes, got {len(raw)}. "
            "Generate one with `python -m hub.manage generate-encryption-key`."
        )
    return raw


@dataclass(frozen=True)
class EnvelopeCipher:
    current: bytes | None = None
    previous: tuple[bytes, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.current is not None

    @classmethod
    def from_config(cls, key: str, previous_keys: str) -> EnvelopeCipher:
        if not key:
            if previous_keys:
                raise EncryptionError(
                    "HUB_ENCRYPTION_KEY_PREVIOUS is set but HUB_ENCRYPTION_KEY is not -- "
                    "a deployment retiring a key still needs a CURRENT one to encrypt "
                    "with going forward. Set HUB_ENCRYPTION_KEY, or clear "
                    "HUB_ENCRYPTION_KEY_PREVIOUS if encryption is being turned off "
                    "entirely (any already-encrypted values will then fail to decrypt)."
                )
            return cls()
        if AESGCM is None:
            raise EncryptionError(
                "HUB_ENCRYPTION_KEY is set but the 'cryptography' package is not installed -- see hub/requirements.txt."
            )
        current = _decode_key(key, source="HUB_ENCRYPTION_KEY")
        previous = tuple(
            _decode_key(item.strip(), source="HUB_ENCRYPTION_KEY_PREVIOUS")
            for item in previous_keys.split(",")
            if item.strip()
        )
        return cls(current=current, previous=previous)

    def encrypt(self, plaintext: str, *, aad: bytes | None = None) -> str:
        if self.current is None:
            return plaintext
        nonce = _secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(self.current).encrypt(nonce, plaintext.encode("utf-8"), aad)
        return (_BOUND_PREFIX if aad is not None else _PREFIX) + base64.urlsafe_b64encode(nonce + ciphertext).decode(
            "ascii"
        )

    def encrypt_subject(self, vault, subject: str, record_id: str, plaintext: str) -> dict:
        """Wrap a subject data key under both the envelope cipher and external vault.

        Subject wrapping keys must stay outside content backups. Destroying only
        a database copy of a wrapped key is not crypto-shredding of its backups.
        """
        if not self.enabled:
            raise EncryptionError("subject envelopes require configured Hub envelope encryption")
        return vault.seal(subject, record_id, plaintext, key_wrapper=self.encrypt)

    def decrypt_subject(self, vault, subject: str, envelope: dict) -> str:
        return vault.open(subject, envelope, key_unwrapper=self.decrypt)

    def decrypt(self, value: str, *, aad: bytes | None = None, allow_legacy: bool = False) -> str:
        bound = value.startswith(_BOUND_PREFIX)
        if bound and aad is None:
            raise EncryptionError("context-bound encryption requires the owning row and field")
        if aad is not None and self.enabled and not bound and not allow_legacy:
            raise EncryptionError(
                "unbound stored field requires `hub.manage bind-encrypted-fields` before serving traffic"
            )
        prefix = _BOUND_PREFIX if bound else _PREFIX
        if not value.startswith(prefix):
            if value.startswith("ctenc:"):
                raise EncryptionError("unsupported encryption envelope version")
            return value
        if AESGCM is None:
            raise EncryptionError(
                "a stored value is an encrypted envelope but the 'cryptography' "
                "package is not installed to decrypt it -- see hub/requirements.txt."
            )
        try:
            raw = base64.b64decode(value[len(prefix) :].encode("ascii"), altchars=b"-_", validate=True)
        except (ValueError, UnicodeError, binascii.Error):
            raise EncryptionError("stored encryption envelope is malformed") from None
        if len(raw) < _NONCE_BYTES + 16:
            raise EncryptionError("stored encryption envelope is truncated")
        nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        candidates = ([self.current] if self.current is not None else []) + list(self.previous)
        for key in candidates:
            try:
                return AESGCM(key).decrypt(nonce, ciphertext, aad if bound else None).decode("utf-8")
            except (InvalidTag, UnicodeDecodeError):  # Try other keys only on authentication/decode failures
                continue
        raise EncryptionError(
            "could not decrypt a stored value with the current HUB_ENCRYPTION_KEY or "
            f"any of the {len(self.previous)} HUB_ENCRYPTION_KEY_PREVIOUS entr"
            f"{'y' if len(self.previous) == 1 else 'ies'} configured. If this followed a "
            "key rotation, confirm the retired key is still listed in "
            "HUB_ENCRYPTION_KEY_PREVIOUS."
        )


NULL_CIPHER = EnvelopeCipher()


async def ensure_bound_fields(session_factory, cipher: EnvelopeCipher) -> None:
    """Refuse a partial secret-format upgrade before starting serving workers."""
    if not cipher.enabled:
        return
    from sqlalchemy import select

    from hub.db import operator_session_scope
    from hub.models import Connector, WebhookEndpoint

    async with operator_session_scope(session_factory) as session:
        for model, field in ((WebhookEndpoint, WebhookEndpoint.url), (Connector, Connector.secret)):
            if (await session.execute(select(model.id).where(~field.startswith(_BOUND_PREFIX)).limit(1))).first():
                raise EncryptionError(
                    "stored secret fields need `python -m hub.manage bind-encrypted-fields` before startup"
                )
