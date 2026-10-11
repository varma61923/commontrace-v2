import pytest

from commontrace.configuration import apply, load


def test_profiles_environment_precedence_and_validation(tmp_path, monkeypatch):
    (tmp_path / "commontrace.yaml").write_text(
        "profile: local\nprofiles:\n  local:\n    embedding: bge-small\n    redact_pii: true\n    telemetry: false\n"
    )
    config = load(str(tmp_path))
    monkeypatch.setenv("COMMONTRACE_FACT_EMBEDDER", "arctic-m")
    with apply(config):
        import os

        from commontrace.configuration import getenv

        assert os.environ["COMMONTRACE_FACT_EMBEDDER"] == "arctic-m"
        assert getenv("COMMONTRACE_REDACT_PII") == "1"
        assert "COMMONTRACE_REDACT_PII" not in os.environ
    (tmp_path / "commontrace.yaml").write_text("profile: unknown\n")
    with pytest.raises(ValueError):
        load(str(tmp_path))


def test_configuration_is_isolated_across_concurrent_requests(tmp_path, monkeypatch):
    import asyncio

    from commontrace.configuration import getenv

    monkeypatch.delenv("COMMONTRACE_FACT_EMBEDDER", raising=False)
    for name in ("arctic-m", "bge-small"):
        root = tmp_path / name
        root.mkdir()
        (root / "commontrace.yaml").write_text("profiles:\n  local:\n    embedding: " + name + "\n")

    async def request(name):
        with apply(load(str(tmp_path / name))):
            await asyncio.sleep(0)
            return getenv("COMMONTRACE_FACT_EMBEDDER")

    async def requests():
        return await asyncio.gather(request("arctic-m"), request("bge-small"))

    assert asyncio.run(requests()) == ["arctic-m", "bge-small"]
    assert getenv("COMMONTRACE_FACT_EMBEDDER") is None


def test_advanced_settings_and_secret_references_share_the_yaml(tmp_path, monkeypatch):
    from commontrace.configuration import getenv
    from commontrace.secrets_provider import env_secret

    monkeypatch.setenv("OWNER_MODEL_KEY", "private-test-key")
    (tmp_path / "commontrace.yaml").write_text(
        "settings:\n  llm:\n    max_tokens: 240\n  server:\n    max_title_chars: 180\n"
        "secrets:\n  COMMONTRACE_LLM_API_KEY: env:OWNER_MODEL_KEY\n"
    )
    with apply(load(str(tmp_path))):
        assert getenv("COMMONTRACE_LLM_MAX_TOKENS") == "240"
        assert getenv("HUB_MAX_TITLE_CHARS") == "180"
        assert env_secret("COMMONTRACE_LLM_API_KEY") == "private-test-key"
    (tmp_path / "commontrace.yaml").write_text("settings:\n  llm:\n    invented_option: true\n")
    with pytest.raises(ValueError, match="setting"):
        load(str(tmp_path))


@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf"])
def test_nonfinite_configuration_rejected(tmp_path, value):
    (tmp_path / "commontrace.yaml").write_text("settings:\n  runtime:\n    warm_idle: " + value + "\n")
    with pytest.raises(ValueError, match="finite"):
        load(str(tmp_path))


def test_store_local_configuration_and_relative_rotating_secret(tmp_path, monkeypatch):
    from commontrace.configuration import configured, getenv
    from commontrace.secrets_provider import env_secret

    monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
    (tmp_path / "key").write_text("first")
    (tmp_path / "commontrace.yaml").write_text(
        "settings:\n  llm:\n    max_tokens: 240\nsecrets:\n  COMMONTRACE_LLM_API_KEY: file:key\n"
    )

    @configured
    def operation(root):
        return getenv("COMMONTRACE_LLM_MAX_TOKENS"), env_secret("COMMONTRACE_LLM_API_KEY")

    assert operation(str(tmp_path)) == ("240", "first")
    (tmp_path / "key").write_text("rotated")
    assert operation(str(tmp_path)) == ("240", "rotated")


def test_plaintext_credentials_are_not_settings(tmp_path):
    (tmp_path / "commontrace.yaml").write_text("settings:\n  llm:\n    api_key: plaintext\n")
    with pytest.raises(ValueError, match="credentials"):
        load(str(tmp_path))


def test_environment_migration_keeps_credentials_out_of_yaml_and_report(tmp_path, monkeypatch):
    import json

    from commontrace.configuration import SETTING_PATHS, migrate_environment

    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMONTRACE_LLM_MAX_TOKENS", "320")
    monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "private-never-copy-this")
    preview = migrate_environment(str(tmp_path))
    assert not preview["written"]
    assert not (tmp_path / "commontrace.yaml").exists()
    result = migrate_environment(str(tmp_path), write=True)
    source = (tmp_path / "commontrace.yaml").read_text()
    assert "private-never-copy-this" not in source + json.dumps(result)
    assert load(str(tmp_path))["secrets"]["COMMONTRACE_LLM_API_KEY"] == "env:COMMONTRACE_LLM_API_KEY"
    assert load(str(tmp_path))["settings"]["llm.max_tokens"] == "320"


def test_nested_store_without_yaml_does_not_inherit_another_stores_routes(tmp_path, monkeypatch):
    from commontrace.configuration import configured, getenv

    monkeypatch.delenv("COMMONTRACE_LLM_MODEL", raising=False)
    outer = tmp_path / "outer"
    inner = tmp_path / "inner"
    outer.mkdir()
    inner.mkdir()
    (outer / "commontrace.yaml").write_text("profiles:\n  local:\n    llm_model: private-model\n")

    @configured
    def operation(root):
        return getenv("COMMONTRACE_LLM_MODEL")

    with apply(load(str(outer))):
        assert operation(str(outer)) == "private-model"
        assert operation(str(inner)) is None
        assert operation(str(outer)) == "private-model"


def test_gateway_worker_retains_explicit_startup_profile_without_mutating_environment(tmp_path, monkeypatch):
    import json
    from concurrent.futures import ThreadPoolExecutor

    from commontrace.configuration import getenv
    from commontrace.gateway import Gateway

    monkeypatch.delenv("COMMONTRACE_LLM_MODEL", raising=False)
    (tmp_path / "commontrace.yaml").write_text(
        "profiles:\n  local:\n    llm_model: local-model\n  team:\n    llm_model: team-model\n"
    )
    with apply(load(str(tmp_path), profile="team")):
        gateway = Gateway(str(tmp_path), durable=False)
    gateway._route(
        "GET",
        "/configuration-probe",
        lambda _body, _query: {"model": getenv("COMMONTRACE_LLM_MODEL")},
        summary="configuration probe",
        auth=False,
    )
    with ThreadPoolExecutor(max_workers=1) as worker:
        response = worker.submit(gateway.handle, "GET", "/configuration-probe").result()
    assert response.status == 200
    assert json.loads(response.body)["model"] == "team-model"
