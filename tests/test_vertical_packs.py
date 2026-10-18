import time

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
