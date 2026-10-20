import time

import pytest

from commontrace import packs


def test_all_vertical_packs_have_docs_and_deterministic_offline_demos():
    listed = packs.list_packs()
    assert {item.name for item in listed} == {
        "coding-agent", "support-outcome", "bank-ai-governance", "healthcare",
        "robot-fleet", "autonomy-safety-case", "home-eldercare-robot", "sovereign-airgapped",
    }
    started = time.perf_counter()
    for item in listed:
        first = packs.demo(item.name, seed=71)
        second = packs.demo(item.name, seed=71)
        assert first == second
        assert set(first["feature_checks"]) == set(item.features)
        assert all(check["passed"] for check in first["feature_checks"].values())
        assert first["network_calls"] == 0 and first["synthetic"] is True
        assert "interval" in first["report"] and "Guarantee:" in first["report"] and "Estimate:" in first["report"]
    assert time.perf_counter() - started < 10


def test_install_copies_versioned_config_and_pack_docs(tmp_path):
    result = packs.install("robot-fleet", str(tmp_path))
    assert result["network_calls"] == 0
    target = tmp_path / "robot-fleet"
    assert (target / "pack.json").is_file()
    assert (target / "README.md").is_file()
    assert '"G28"' in (target / "pack.json").read_text()


def test_sovereign_airgapped_pack_disables_network_dependent_adapters():
    from commontrace.packs import _load

    data, _ = _load("sovereign-airgapped")
    assert data["defaults"]["network_features_enabled"] is False
    assert {"hub", "remote-models", "external-kms", "telemetry", "vendor-adapters"} <= set(
        data["defaults"]["disabled_features"]
    )


def test_pack_install_cannot_follow_a_child_symlink_outside_the_destination(tmp_path):
    root = tmp_path / "destination"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "robot-fleet").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        packs.install("robot-fleet", str(root))
    assert not list(outside.iterdir())


@pytest.mark.parametrize("name", [None, 1, "../robot-fleet", "robot-fleet/child"])
def test_pack_names_are_validated_without_path_coercion(name):
    with pytest.raises(ValueError, match="name"):
        packs.demo(name)


def test_pack_demo_executes_real_shield_and_does_not_mask_failure(monkeypatch):
    from commontrace.robotics import shield

    def unavailable(*args, **kwargs):
        raise RuntimeError("shield unavailable")

    monkeypatch.setattr(shield, "check", unavailable)
    with pytest.raises(RuntimeError, match="shield unavailable"):
        packs.demo("robot-fleet")


def test_pack_demos_do_not_use_network_even_with_tracing_configured(monkeypatch):
    import socket

    def unexpected_network(*args, **kwargs):
        raise AssertionError("offline demo attempted network I/O")

    monkeypatch.setenv("COMMONTRACE_OTEL", "1")
    monkeypatch.setattr(socket, "create_connection", unexpected_network)
    monkeypatch.setattr(socket, "getaddrinfo", unexpected_network)
    for item in packs.list_packs():
        assert packs.demo(item.name)["network_calls"] == 0


def test_scoped_offline_policy_resets_without_affecting_other_threads(monkeypatch):
    import os
    from concurrent.futures import ThreadPoolExecutor

    from commontrace import offline

    monkeypatch.delenv("COMMONTRACE_OFFLINE", raising=False)
    before = dict(os.environ)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(RuntimeError, match="cleanup"):
            with offline.without_network():
                assert offline.enabled()
                assert not executor.submit(offline.enabled).result(timeout=5)
                for url in ("https://example.com", "http://localhost:8000"):
                    with pytest.raises(offline.OfflineError):
                        offline.check_url(url, "demo")
                with offline.without_network():
                    assert offline.enabled()
                offline.apply_environment()
                assert offline.enabled()
                raise RuntimeError("cleanup")
    assert not offline.enabled()
    assert dict(os.environ) == before


def test_cached_tracer_honors_offline_policy_and_enable_switch(monkeypatch):
    from commontrace import offline, telemetry

    monkeypatch.delenv("COMMONTRACE_OFFLINE", raising=False)
    monkeypatch.setenv("COMMONTRACE_OTEL", "1")
    tracer = object()
    monkeypatch.setattr(telemetry, "_TRACER", tracer)
    monkeypatch.setattr(telemetry, "_TRACER_STATE", {"tried": True, "error": ""})
    assert telemetry._tracer() is tracer
    with offline.without_network():
        assert telemetry._tracer() is None
        assert telemetry.status()["tracing"] is False
    assert telemetry._tracer() is tracer
    monkeypatch.setenv("COMMONTRACE_OTEL", "0")
    assert telemetry._tracer() is None
