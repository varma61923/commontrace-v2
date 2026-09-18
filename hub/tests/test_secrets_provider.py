"""hub/secrets_provider.py: the `{NAME}_FILE` indirection that lets any
secret manager capable of writing a file (Vault Agent, a cloud provider's
Secrets Store CSI driver, Kubernetes Secret volumes, Docker secrets)
supply a HUB_ secret with no vendor SDK and no other code change -- see
that module's docstring for the full reasoning."""
from __future__ import annotations

import pytest

from hub.secrets_provider import env_secret


class TestPlainEnvVar:
    def test_reads_the_plain_env_var_when_no_file_variant_is_set(self, monkeypatch):
        monkeypatch.delenv("HUB_TEST_SECRET_FILE", raising=False)
        monkeypatch.setenv("HUB_TEST_SECRET", "plain-value")
        assert env_secret("HUB_TEST_SECRET") == "plain-value"

    def test_default_when_neither_is_set(self, monkeypatch):
        monkeypatch.delenv("HUB_TEST_SECRET", raising=False)
        monkeypatch.delenv("HUB_TEST_SECRET_FILE", raising=False)
        assert env_secret("HUB_TEST_SECRET", "fallback") == "fallback"

    def test_default_is_empty_string_unless_given(self, monkeypatch):
        monkeypatch.delenv("HUB_TEST_SECRET", raising=False)
        monkeypatch.delenv("HUB_TEST_SECRET_FILE", raising=False)
        assert env_secret("HUB_TEST_SECRET") == ""


class TestFileIndirection:
    def test_reads_from_the_file_named_by_the_file_variant(self, monkeypatch, tmp_path):
        secret_file = tmp_path / "secret"
        secret_file.write_text("file-value\n")
        monkeypatch.delenv("HUB_TEST_SECRET", raising=False)
        monkeypatch.setenv("HUB_TEST_SECRET_FILE", str(secret_file))
        assert env_secret("HUB_TEST_SECRET") == "file-value"

    def test_strips_surrounding_whitespace(self, monkeypatch, tmp_path):
        secret_file = tmp_path / "secret"
        secret_file.write_text("  padded-value  \n\n")
        monkeypatch.setenv("HUB_TEST_SECRET_FILE", str(secret_file))
        assert env_secret("HUB_TEST_SECRET") == "padded-value"

    def test_the_file_variant_wins_over_a_plain_env_var_set_alongside_it(
        self, monkeypatch, tmp_path
    ):
        """A deployment that wired up a real secret store must never
        silently fall back to a stale plaintext value left in the plain
        env var from an earlier configuration."""
        secret_file = tmp_path / "secret"
        secret_file.write_text("from-the-secret-store")
        monkeypatch.setenv("HUB_TEST_SECRET", "stale-plaintext-value")
        monkeypatch.setenv("HUB_TEST_SECRET_FILE", str(secret_file))
        assert env_secret("HUB_TEST_SECRET") == "from-the-secret-store"

    def test_a_missing_file_raises_a_clear_error_naming_the_variable_and_path(
        self, monkeypatch, tmp_path
    ):
        missing = tmp_path / "does-not-exist"
        monkeypatch.setenv("HUB_TEST_SECRET_FILE", str(missing))
        with pytest.raises(RuntimeError, match="HUB_TEST_SECRET_FILE") as exc:
            env_secret("HUB_TEST_SECRET")
        assert str(missing) in str(exc.value)

    def test_an_empty_file_variant_falls_back_to_the_plain_env_var(self, monkeypatch):
        """Empty string and unset both mean "no file variant" -- matching
        every other _env_* helper in hub/config.py, where an empty env var
        is treated the same as an absent one."""
        monkeypatch.setenv("HUB_TEST_SECRET_FILE", "")
        monkeypatch.setenv("HUB_TEST_SECRET", "plain-value")
        assert env_secret("HUB_TEST_SECRET") == "plain-value"
