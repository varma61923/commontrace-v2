"""Bring-your-own-key: the at-rest key is unwrapped by the customer's KMS at start-up, refused cleanly when the KMS
refuses, and never run without. A fake KMS stands in for boto3's client (the request shapes are the SDK's own)."""
import base64
import os

import pytest

from hub import encryption, kms
from hub.config import HubConfig

pytestmark = pytest.mark.asyncio


class FakeKMS:
    """What `kms.decrypt` / `kms.generate_data_key` look like. A "wrapped" blob is the key XORed with a
    per-key pad, which only this fake can undo, bound to the encryption context it was made with."""

    def __init__(self, allowed=True):
        self.allowed, self.calls = allowed, []

    def generate_data_key(self, **kw):
        assert kw["KeySpec"] == "AES_256" and kw["EncryptionContext"] == kms.CONTEXT
        plaintext = os.urandom(32)
        return {"Plaintext": plaintext, "CiphertextBlob": b"WRAP|" + kw["KeyId"].encode() + b"|" + plaintext[::-1]}

    def decrypt(self, **kw):
        self.calls.append(kw)
        if not self.allowed:
            raise RuntimeError("AccessDeniedException")
        assert kw["EncryptionContext"] == kms.CONTEXT
        _tag, key_id, rest = kw["CiphertextBlob"].split(b"|", 2)
        if kw.get("KeyId") and kw["KeyId"].encode() != key_id:
            raise RuntimeError("IncorrectKeyException")
        return {"Plaintext": rest[::-1]}


def _wrap(fake, key_id="arn:key/1"):
    return base64.b64encode(fake.generate_data_key(KeyId=key_id, KeySpec="AES_256",
                                                   EncryptionContext=kms.CONTEXT)["CiphertextBlob"]).decode()


async def test_the_unwrapped_key_works_as_the_hub_encryption_key():
    fake = FakeKMS()
    wrapped = kms.generate_wrapped("arn:key/1", client=fake)
    key = kms.unwrap(wrapped, key_id="arn:key/1", client=fake)
    cipher = encryption.EnvelopeCipher.from_config(key, "")
    assert cipher.decrypt(cipher.encrypt("https://hooks.example/secret-path")) == "https://hooks.example/secret-path"
    assert fake.calls[0]["EncryptionContext"] == {"purpose": "commontrace-hub-encryption-key"}


async def test_only_the_wrapped_form_is_ever_returned():
    fake = FakeKMS()
    out = kms.generate_wrapped("arn:key/1", client=fake)
    assert isinstance(out, str) and base64.b64decode(out)           # a blob, not a 32-byte key to print


async def test_a_blob_for_another_key_or_a_revoked_grant_stops_start_up_with_a_clear_message():
    fake = FakeKMS()
    wrapped = kms.generate_wrapped("arn:key/other", client=fake)
    with pytest.raises(encryption.EncryptionError, match="refused to unwrap"):
        kms.unwrap(wrapped, key_id="arn:key/1", client=fake)
    with pytest.raises(encryption.EncryptionError, match="revoked access"):
        kms.unwrap(wrapped, client=FakeKMS(allowed=False))


@pytest.mark.parametrize("bad", ["not base64 !!!", "QUJD"])
async def test_garbage_is_refused(bad):
    with pytest.raises(encryption.EncryptionError):
        kms.unwrap(bad, client=FakeKMS())


async def test_a_key_of_the_wrong_size_is_refused():
    class Short(FakeKMS):
        def decrypt(self, **kw):
            return {"Plaintext": b"short"}

    with pytest.raises(encryption.EncryptionError, match="expected 32"):
        kms.unwrap(base64.b64encode(b"x").decode(), client=Short())


async def test_rotation_keeps_old_values_readable(monkeypatch):
    fake = FakeKMS()
    monkeypatch.setattr(kms, "_client", lambda region: fake)
    old_wrapped = kms.generate_wrapped("arn:key/1", client=fake)
    new_wrapped = kms.generate_wrapped("arn:key/1", client=fake)
    old_key = kms.unwrap(old_wrapped, client=fake)
    written = encryption.EnvelopeCipher.from_config(old_key, "").encrypt("secret")
    env = {"HUB_ENCRYPTION_KEY_KMS_WRAPPED": new_wrapped, "HUB_ENCRYPTION_KEY_PREVIOUS_KMS_WRAPPED": old_wrapped,
           "HUB_KMS_KEY_ID": "arn:key/1"}
    current, previous = kms.resolve(env, lambda name: env.get(name, ""))
    assert encryption.EnvelopeCipher.from_config(current, previous).decrypt(written) == "secret"


@pytest.mark.parametrize("env,message", [
    ({"HUB_ENCRYPTION_KEY": "a", "HUB_ENCRYPTION_KEY_KMS_WRAPPED": "b"}, "not both"),
    ({"HUB_ENCRYPTION_KEY_PREVIOUS": "a", "HUB_ENCRYPTION_KEY_PREVIOUS_KMS_WRAPPED": "b"}, "not both")])
async def test_plain_and_wrapped_cannot_be_mixed(env, message):
    with pytest.raises(encryption.EncryptionError, match=message):
        kms.resolve(env, lambda name: env.get(name, ""))


async def test_no_byok_settings_means_the_plain_path_unchanged():
    env = {"HUB_ENCRYPTION_KEY": "plainkey", "HUB_ENCRYPTION_KEY_PREVIOUS": "old"}
    assert kms.resolve(env, lambda name: env.get(name, "")) == ("plainkey", "old")
    assert kms.resolve({}, lambda name: "") == ("", "")


async def test_the_config_resolves_the_wrapped_key_once_at_start_up(monkeypatch):
    fake = FakeKMS()
    monkeypatch.setattr(kms, "_client", lambda region: fake)
    wrapped = kms.generate_wrapped("arn:key/1", client=fake)
    for k, v in {"HUB_DATABASE_URL": "postgresql+asyncpg://u:p@h/db", "HUB_ENCRYPTION_KEY_KMS_WRAPPED": wrapped,
                 "HUB_KMS_KEY_ID": "arn:key/1", "HUB_KMS_REGION": "eu-west-1"}.items():
        monkeypatch.setenv(k, v)
    for k in ("HUB_ENCRYPTION_KEY", "HUB_ENCRYPTION_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    config = HubConfig.from_env()
    assert config.cipher().enabled and len(fake.calls) == 1


async def test_a_missing_sdk_is_named_and_the_hub_does_not_start_unencrypted(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(encryption.EncryptionError, match="boto3 is not installed"):
        kms.unwrap(base64.b64encode(b"x" * 40).decode())
