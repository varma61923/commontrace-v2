"""The local runtime image: pinned, non-root, minimal context, and wired into compose."""
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_local_image_is_pinned_non_root_and_health_checked():
    dockerfile = (ROOT / "Dockerfile.local").read_text()
    images = re.findall(r"^FROM\s+(\S+)", dockerfile, flags=re.M)
    assert images and all(re.search(r"@sha256:[0-9a-f]{64}$", i) for i in images)
    assert re.search(r"^USER commontrace$", dockerfile, flags=re.M)
    assert "/v1/health/live" in dockerfile and "COMMONTRACE_ROOT=/data" in dockerfile


def test_build_context_ships_only_the_package():
    ignore = (ROOT / "Dockerfile.local.dockerignore").read_text().splitlines()
    assert "*" in ignore  # deny by default
    allowed = {line[1:] for line in ignore if line.startswith("!")}
    lines = [line for line in (ROOT / "Dockerfile.local").read_text().splitlines()
             if line.startswith("COPY ") and "--from=" not in line]
    assert lines
    for line in lines:
        *sources, _destination = [part for part in line.split()[1:] if not part.startswith("--")]
        for source in sources:
            assert any(source.rstrip("/") == a.rstrip("/") for a in allowed), f"{source} is not in the context"
    assert not {"memory/", "tests/", "hub/", "benchmarks/"} & allowed


def test_compose_local_profile_shares_one_store_and_publishes_on_loopback_only():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    for name in ("gateway", "mcp"):
        service = services[name]
        assert service["profiles"] == ["local"] and service["build"]["dockerfile"] == "Dockerfile.local"
        assert "local_store:/data" in service["volumes"]
        assert all(port.startswith("127.0.0.1:") for port in service["ports"])
        assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
    assert "--allow-insecure-http" in services["mcp"]["command"]
    for name in ("db", "migrate", "hub"):
        assert "profiles" not in services[name]  # `docker compose up` is unchanged
