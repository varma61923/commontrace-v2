"""Envelope encryption for the handful of at-rest fields where it is safe.

WHY THIS DOES NOT TOUCH Trace.title / context_text / solution_text
--------------------------------------------------------------------
hub/models.py's `Trace.search_vector` is a Postgres GENERATED STORED column,
computed by Postgres itself directly from those three columns
(`to_tsvector('english', title || ' ' || context_text || ' ' || solution_text)`).
Encrypting them at the application layer would mean Postgres builds that
tsvector from ciphertext -- `search_traces` would still run without error,
it would just never match anything, silently, forever. A search feature
that quietly stops finding results is worse than no encryption at all,
because nothing signals that it broke.

The same conflict rules out `Trace.subject_ids`: `find_traces_by_subject`/
`purge_traces_by_subject` (hub/crud.py) rely on exact array-membership
matches against a GIN index, which application-layer encryption (with a
fresh nonce per value, as this module deliberately uses -- see `encrypt`)
makes impossible, since the same subject id would encrypt to a different
ciphertext every time.

The right place to encrypt content a database has to compute over --
full-text search, exact-match array containment -- is the storage layer
underneath Postgres: a managed provider's encryption-at-rest (RDS/Cloud
SQL), an encrypted filesystem (LUKS), or a Postgres TDE extension, none of
which this application can configure on an operator's behalf. See
hub/DEPLOYMENT.md's "Encryption at rest" section for what to set up there.
SOC2_READINESS.md's Confidentiality table documents this as a deliberate
split rather than leaving "what about at-rest encryption?" unanswered.

WHAT THIS DOES TOUCH
---------------------
Fields the database never computes over and never matches with a partial
or exact predicate -- currently `WebhookEndpoint.url` (hub/events.py). A
webhook URL is often unique to one org, is never searched, and sometimes
carries a bearer token or shared secret in its path or query string --
exactly the kind of value that should not sit in plaintext in a Postgres
dump or a `pg_dump` backup. Encrypting it costs nothing functionally: the
column is only ever read back whole, to deliver a webhook or to show an
operator what they registered.

OPT-IN, LIKE EVERYTHING ELSE OF THIS SHAPE IN hub/config.py
--------------------------------------------------------------
No `HUB_ENCRYPTION_KEY` set -> `cipher().enabled` is False -> `encrypt`/
`decrypt` are the identity function. An existing deployment that never sets
this env var is completely unaffected: `WebhookEndpoint.url` keeps being
stored as plaintext, exactly as it always was. This matches `oidc_issuer`,
`admin_token`, `console_secret`, and `stripe_secret_key` in hub/config.py:
an operator opts in to each capability by setting its config, and the
absence of a value means "this deployment does not use this," never
"this deployment forgot to configure something."

KEY ROTATION
------------
`HUB_ENCRYPTION_KEY_PREVIOUS` is a comma-separated list of retired keys,
kept only so a value encrypted under one of them can still be decrypted.
`encrypt` always uses the CURRENT key; nothing here re-encrypts existing
rows under a new key automatically -- a rotation is "the current key
changed" until something re-writes each row, which for `WebhookEndpoint`
happens naturally the next time an operator re-registers or rotates that
endpoint's signing secret.
"""

from __future__ import annotations

import base64
import secrets as _secrets
from dataclasses import dataclass

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ModuleNotFoundError:  # pragma: no cover - exercised when cryptography isn't installed
    AESGCM = None  # type: ignore[assignment,misc]

_PREFIX = "ctenc:v1:"
_KEY_BYTES = 32
_NONCE_BYTES = 12


class EncryptionError(ValueError):
    """A configured HUB_ENCRYPTION_KEY (or a HUB_ENCRYPTION_KEY_PREVIOUS
    entry) is malformed, or a stored envelope could not be decrypted with
    any configured key.

    A ValueError subclass, like every other HubConfig field-format error
    (hub/config.py's `_env_int_in_range`, `rate_limit_backend`) -- this is
    raised eagerly from `HubConfig.__post_init__`, so `hub/manage.py`'s
    existing `except (ValueError, LookupError)` in `main()` already reports
    it as a clean operator-facing message instead of a raw traceback."""


def generate_key() -> str:
    """A fresh, correctly-sized key for HUB_ENCRYPTION_KEY. This is what
    `hub.manage generate-encryption-key` prints -- not used internally,
    since this module never generates a key on an operator's behalf."""
    return base64.urlsafe_b64encode(_secrets.token_bytes(_KEY_BYTES)).decode("ascii")


def _decode_key(value: str, *, source: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise EncryptionError(f"{source} is not valid urlsafe-base64: {exc}") from None
    if len(raw) != _KEY_BYTES:
        raise EncryptionError(
            f"{source} must decode to exactly {_KEY_BYTES} bytes, got {len(raw)}. "
            "Generate one with `python -m hub.manage generate-encryption-key`."
        )
    return raw


@dataclass(frozen=True)
class EnvelopeCipher:
    """AES-256-GCM with one active key plus zero or more retired keys kept
    only for decrypting data written before a rotation.

    A disabled cipher (`current is None`, the default) makes both
    directions the identity function -- see the module docstring's "opt-in"
    section for why that, not an exception, is the right behavior for an
    unconfigured deployment.
    """

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
                "HUB_ENCRYPTION_KEY is set but the 'cryptography' package is not "
                "installed -- see hub/requirements.txt."
            )
        current = _decode_key(key, source="HUB_ENCRYPTION_KEY")
        previous = tuple(
            _decode_key(item.strip(), source="HUB_ENCRYPTION_KEY_PREVIOUS")
            for item in previous_keys.split(",") if item.strip()
        )
        return cls(current=current, previous=previous)

    def encrypt(self, plaintext: str) -> str:
        if self.current is None:
            return plaintext
        nonce = _secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(self.current).encrypt(nonce, plaintext.encode("utf-8"), None)
        return _PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, value: str) -> str:
        """Legacy plaintext (no envelope prefix) passes through unchanged,
        the same as it would with a disabled cipher -- a value written
        before encryption was enabled must stay readable after."""
        if not value.startswith(_PREFIX):
            return value
        if AESGCM is None:
            raise EncryptionError(
                "a stored value is an encrypted envelope but the 'cryptography' "
                "package is not installed to decrypt it -- see hub/requirements.txt."
            )
        raw = base64.urlsafe_b64decode(value[len(_PREFIX):].encode("ascii"))
        nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        candidates = ([self.current] if self.current is not None else []) + list(self.previous)
        for key in candidates:
            try:
                return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
            except Exception:  # noqa: BLE001 - try every configured key before giving up
                continue
        raise EncryptionError(
            "could not decrypt a stored value with the current HUB_ENCRYPTION_KEY or "
            f"any of the {len(self.previous)} HUB_ENCRYPTION_KEY_PREVIOUS entr"
            f"{'y' if len(self.previous) == 1 else 'ies'} configured. If this followed a "
            "key rotation, confirm the retired key is still listed in "
            "HUB_ENCRYPTION_KEY_PREVIOUS."
        )


#: Shared disabled instance for call sites that accept an optional cipher --
#: identical in behavior to `EnvelopeCipher()`, spelled out so it's obvious
#: at each call site that "no cipher given" and "encryption disabled" are
#: the same state, not two different defaults to reconcile.
NULL_CIPHER = EnvelopeCipher()
