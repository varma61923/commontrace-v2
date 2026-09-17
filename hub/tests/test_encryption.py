"""hub/encryption.py: the envelope cipher WebhookEndpoint.url is stored
under when HUB_ENCRYPTION_KEY is set, and the identity function it must be
when it isn't -- see that module's docstring for why Trace content is
deliberately never touched by this."""
from __future__ import annotations

import base64

import pytest

from hub.encryption import (
    _PREFIX,
    NULL_CIPHER,
    EncryptionError,
    EnvelopeCipher,
    generate_key,
)


def test_generate_key_is_a_valid_key():
    key = generate_key()
    cipher = EnvelopeCipher.from_config(key, "")
    assert cipher.enabled


def test_generate_key_produces_distinct_keys():
    assert generate_key() != generate_key()


class TestDisabledByDefault:
    def test_null_cipher_is_disabled(self):
        assert NULL_CIPHER.enabled is False

    def test_empty_config_is_disabled(self):
        assert EnvelopeCipher.from_config("", "").enabled is False

    def test_disabled_encrypt_is_identity(self):
        assert NULL_CIPHER.encrypt("https://example.invalid/hook") == "https://example.invalid/hook"

    def test_disabled_decrypt_of_plaintext_is_identity(self):
        # Legacy/unencrypted rows (or a deployment that never opted in)
        # must keep reading back exactly as written.
        assert NULL_CIPHER.decrypt("https://example.invalid/hook") == "https://example.invalid/hook"


class TestRoundTrip:
    def test_encrypt_then_decrypt_recovers_the_plaintext(self):
        cipher = EnvelopeCipher.from_config(generate_key(), "")
        plaintext = "https://example.invalid/hooks/commontrace?token=s3cr3t"
        assert cipher.decrypt(cipher.encrypt(plaintext)) == plaintext

    def test_ciphertext_does_not_contain_the_plaintext(self):
        cipher = EnvelopeCipher.from_config(generate_key(), "")
        plaintext = "https://example.invalid/hooks/commontrace?token=s3cr3t"
        envelope = cipher.encrypt(plaintext)
        assert plaintext not in envelope
        assert "s3cr3t" not in envelope

    def test_two_encryptions_of_the_same_value_differ(self):
        # A fresh nonce per call -- otherwise two orgs registering the same
        # URL would be linkable from ciphertext alone.
        cipher = EnvelopeCipher.from_config(generate_key(), "")
        plaintext = "https://example.invalid/hook"
        assert cipher.encrypt(plaintext) != cipher.encrypt(plaintext)

    def test_a_value_encrypted_while_disabled_stays_plaintext_once_enabled(self):
        # Simulates a deployment that had no key, wrote plaintext rows, and
        # only later set HUB_ENCRYPTION_KEY. Those old rows must not need
        # a migration to keep reading correctly.
        legacy_value = NULL_CIPHER.encrypt("https://example.invalid/hook")
        cipher = EnvelopeCipher.from_config(generate_key(), "")
        assert cipher.decrypt(legacy_value) == "https://example.invalid/hook"


class TestTamperDetection:
    def test_flipped_ciphertext_byte_fails_to_decrypt(self):
        cipher = EnvelopeCipher.from_config(generate_key(), "")
        envelope = cipher.encrypt("https://example.invalid/hook")
        raw = bytearray(base64.urlsafe_b64decode(envelope[len(_PREFIX):]))
        raw[-1] ^= 0xFF  # corrupt the last ciphertext byte
        tampered = _PREFIX + base64.urlsafe_b64encode(bytes(raw)).decode("ascii")
        with pytest.raises(EncryptionError):
            cipher.decrypt(tampered)


class TestKeyRotation:
    def test_previous_key_still_decrypts_after_rotation(self):
        old_key = generate_key()
        new_key = generate_key()
        old_cipher = EnvelopeCipher.from_config(old_key, "")
        envelope = old_cipher.encrypt("https://example.invalid/hook")

        rotated = EnvelopeCipher.from_config(new_key, old_key)
        assert rotated.decrypt(envelope) == "https://example.invalid/hook"

    def test_new_values_are_encrypted_under_the_current_key_not_a_previous_one(self):
        old_key = generate_key()
        new_key = generate_key()
        rotated = EnvelopeCipher.from_config(new_key, old_key)
        envelope = rotated.encrypt("https://example.invalid/hook")

        # A cipher that only knows the OLD key must not be able to read it.
        old_only = EnvelopeCipher.from_config(old_key, "")
        with pytest.raises(EncryptionError):
            old_only.decrypt(envelope)

    def test_multiple_previous_keys_are_all_tried(self):
        key_a, key_b, key_c = generate_key(), generate_key(), generate_key()
        envelope_a = EnvelopeCipher.from_config(key_a, "").encrypt("https://example.invalid/hook")
        envelope_b = EnvelopeCipher.from_config(key_b, "").encrypt("https://example.invalid/hook")

        current = EnvelopeCipher.from_config(key_c, f"{key_a},{key_b}")
        assert current.decrypt(envelope_a) == "https://example.invalid/hook"
        assert current.decrypt(envelope_b) == "https://example.invalid/hook"

    def test_decrypt_with_no_matching_key_raises(self):
        envelope = EnvelopeCipher.from_config(generate_key(), "").encrypt("https://example.invalid/hook")
        unrelated = EnvelopeCipher.from_config(generate_key(), "")
        with pytest.raises(EncryptionError):
            unrelated.decrypt(envelope)


class TestConfigValidation:
    def test_previous_without_current_is_rejected(self):
        with pytest.raises(EncryptionError):
            EnvelopeCipher.from_config("", generate_key())

    def test_malformed_key_is_rejected(self):
        with pytest.raises(EncryptionError):
            EnvelopeCipher.from_config("not-valid-base64!!!", "")

    def test_wrong_length_key_is_rejected(self):
        too_short = base64.urlsafe_b64encode(b"short").decode("ascii")
        with pytest.raises(EncryptionError):
            EnvelopeCipher.from_config(too_short, "")

    def test_encryption_error_is_a_value_error(self):
        # hub/manage.py's main() relies on this: every other HubConfig
        # field-format error is a ValueError, and this needs the same
        # clean "error: ..." handling rather than a raw traceback.
        assert issubclass(EncryptionError, ValueError)
