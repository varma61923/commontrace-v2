"""Network failures must not weaken the pinned generator's integrity boundary."""

import hashlib
import io
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts import generate_sdks


def test_temporary_maven_failure_recovers_without_changing_checksum(tmp_path, monkeypatch):
    content = b"pinned generator fixture"
    monkeypatch.setattr(generate_sdks, "SHA256", hashlib.sha256(content).hexdigest())
    attempts = []
    delays = []
    monkeypatch.setattr(generate_sdks.time, "sleep", delays.append)

    def response(url, **_):
        attempts.append(url)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(url, 404, "temporary mirror miss", {}, None)
        return io.BytesIO(content)

    monkeypatch.setattr(generate_sdks.urllib.request, "urlopen", response)
    path = tmp_path / "generator.jar"
    assert generate_sdks.checked_jar(path).read_bytes() == content
    assert len(attempts) == 2 and delays == [1]
    assert not path.with_suffix(".download").exists()


def test_checksum_failure_is_never_retried_or_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_sdks.urllib.request, "urlopen", lambda *_, **__: io.BytesIO(b"corrupt"))
    path = tmp_path / "generator.jar"
    with pytest.raises(ValueError, match="checksum"):
        generate_sdks.checked_jar(path)
    assert not path.exists() and not path.with_suffix(".download").exists()


def test_concurrent_generator_downloads_install_the_same_verified_bytes(tmp_path, monkeypatch):
    content = b"verified generator"
    monkeypatch.setattr(generate_sdks, "SHA256", hashlib.sha256(content).hexdigest())
    barrier = threading.Barrier(2)

    class Response(io.BytesIO):
        def read(self, size=-1):
            if self.tell() == 0:
                barrier.wait(timeout=10)
            return super().read(size)

    monkeypatch.setattr(generate_sdks.urllib.request, "urlopen", lambda *_, **__: Response(content))
    path = tmp_path / "generator.jar"
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(generate_sdks.checked_jar, path) for _ in range(2)]
        assert [future.result(timeout=15) for future in futures] == [path, path]
    assert path.read_bytes() == content
    assert list(tmp_path.iterdir()) == [path]


def test_preexisting_download_symlink_cannot_overwrite_local_data(tmp_path, monkeypatch):
    content = b"verified generator"
    monkeypatch.setattr(generate_sdks, "SHA256", hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(generate_sdks.urllib.request, "urlopen", lambda *_, **__: io.BytesIO(content))
    protected = tmp_path / "keep.txt"
    protected.write_bytes(b"private local data")
    path = tmp_path / "generator.jar"
    path.with_suffix(".download").symlink_to(protected)
    assert generate_sdks.checked_jar(path) == path
    assert protected.read_bytes() == b"private local data"
    assert not path.is_symlink() and path.read_bytes() == content


def test_oversized_cached_generator_is_refused_without_network(tmp_path, monkeypatch):
    path = tmp_path / "generator.jar"
    with path.open("wb") as stream:
        stream.truncate(65 * 1024 * 1024)
    monkeypatch.setattr(
        generate_sdks.urllib.request, "urlopen", lambda *_, **__: pytest.fail("cache must be checked locally")
    )
    with pytest.raises(ValueError, match="bounded download size"):
        generate_sdks.checked_jar(path)


def test_typescript_generator_prevents_model_and_parameter_name_collisions(tmp_path, monkeypatch):
    from scripts import generate_sdks

    calls = []
    monkeypatch.setattr(generate_sdks.subprocess, "run", lambda args, **kwargs: calls.append(args))
    generate_sdks.generate(
        ["java", "-jar", "verified.jar"],
        tmp_path / "spec.json",
        "typescript",
        tmp_path / "sdk",
        package_version="2.3.4-rc.1",
    )
    options = calls[0][calls[0].index("--additional-properties") + 1]
    assert calls[0][calls[0].index("--git-user-id") + 1] == "varma61923"
    assert calls[0][calls[0].index("--git-repo-id") + 1] == "unimem"
    assert "npmVersion=2.3.4-rc.1" in options
    assert "prefixParameterInterfaces=true" in options
    assert "licenseName=AGPL-3.0-only" in options


@pytest.mark.parametrize("version", ["latest", "1.2", "01.2.3", "1.2.3,npmName=evil", "$(command)", "1.2.3\n"])
def test_invalid_release_versions_are_rejected_before_downloading(tmp_path, monkeypatch, version):
    from scripts import generate_sdks

    monkeypatch.setattr("sys.argv", ["generate_sdks", "--package-version", version])
    monkeypatch.setattr(generate_sdks, "checked_jar", lambda path: pytest.fail("must validate before network"))
    with pytest.raises(SystemExit):
        generate_sdks.main()


def test_kotlin_transport_repair_removes_only_the_unreachable_pinned_branch(tmp_path):
    source = tmp_path / "src/main/kotlin/io/unimem/infrastructure/ApiClient.kt"
    source.parent.mkdir(parents=True)
    original = "before\nval body = response.body ?: return null\nafter\n"
    source.write_text(original)
    generate_sdks.normalize_kotlin_transport(tmp_path)
    assert source.read_text() == "before\nval body = response.body\nafter\n"
    with pytest.raises(ValueError, match="template changed"):
        generate_sdks.normalize_kotlin_transport(tmp_path)
    assert source.read_text() == "before\nval body = response.body\nafter\n"
