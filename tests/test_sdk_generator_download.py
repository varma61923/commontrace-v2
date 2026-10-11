"""Network failures must not weaken the pinned generator's integrity boundary."""

import hashlib
import io
import urllib.error

import pytest

from scripts import generate_sdks


def test_temporary_maven_failure_recovers_without_changing_checksum(tmp_path, monkeypatch):
    content = b"pinned generator fixture"
    monkeypatch.setattr(generate_sdks, "SHA256", hashlib.sha256(content).hexdigest())
    attempts = []

    def response(url, **_):
        attempts.append(url)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(url, 404, "temporary mirror miss", {}, None)
        return io.BytesIO(content)

    monkeypatch.setattr(generate_sdks.urllib.request, "urlopen", response)
    path = tmp_path / "generator.jar"
    assert generate_sdks.checked_jar(path).read_bytes() == content
    assert len(attempts) == 2
    assert not path.with_suffix(".download").exists()


def test_checksum_failure_is_never_retried_or_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_sdks.urllib.request, "urlopen", lambda *_, **__: io.BytesIO(b"corrupt"))
    path = tmp_path / "generator.jar"
    with pytest.raises(ValueError, match="checksum"):
        generate_sdks.checked_jar(path)
    assert not path.exists() and not path.with_suffix(".download").exists()
