import os

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


@pytest.mark.parametrize("override", [False, True])
def test_oauth_resolves_yaml_once_and_preserves_explicit_environment_precedence(tmp_path, monkeypatch, override):
    from commontrace import oauth
    from commontrace.configuration import SETTING_PATHS

    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "commontrace.yaml").write_text(
        "settings:\n  runtime:\n    oauth_issuer: https://yaml.example/\n"
        "    oauth_audience: memory-api\n    oauth_jwks_url: https://yaml.example/keys\n"
        "    oauth_algorithms: EdDSA,RS256\n    oauth_leeway: 17\n"
    )
    if override:
        monkeypatch.setenv("COMMONTRACE_OAUTH_ISSUER", "https://override.example/")
        monkeypatch.setenv("COMMONTRACE_OAUTH_LEEWAY", "23")
    with apply(load(str(tmp_path))):
        resolved = oauth.load_config(str(tmp_path))
    assert resolved.issuer == ("https://override.example/" if override else "https://yaml.example/")
    assert resolved.audience == "memory-api"
    assert resolved.jwks_url == "https://yaml.example/keys"
    assert resolved.algorithms == ("EdDSA", "RS256")
    assert resolved.leeway == (23 if override else 17)
    assert "COMMONTRACE_OAUTH_AUDIENCE" not in os.environ


def test_invalid_yaml_oauth_still_fails_at_the_existing_validation_boundary(tmp_path, monkeypatch):
    from commontrace import oauth
    from commontrace.configuration import SETTING_PATHS
    from commontrace.exceptions import ConfigurationError

    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "commontrace.yaml").write_text(
        "settings:\n  runtime:\n    oauth_issuer: https://issuer.example/\n"
        "    oauth_audience: memory-api\n    oauth_jwks_url: https://issuer.example/keys\n"
        "    oauth_leeway: malformed\n"
    )
    with apply(load(str(tmp_path))), pytest.raises(ConfigurationError, match="leeway must be an integer"):
        oauth.load_config(str(tmp_path))


@pytest.mark.parametrize("override", [False, True])
def test_hub_kms_resolves_routing_and_rotating_secret_from_yaml(tmp_path, monkeypatch, override):
    import base64

    from commontrace.configuration import SETTING_PATHS
    from hub import kms
    from hub.config import HubConfig

    for name in SETTING_PATHS:
        for suffix in ("", "_FILE", "_AWS_SECRET_ID", "_AWS_SECRET_FIELD", "_AWS_REGION"):
            monkeypatch.delenv(name + suffix, raising=False)
    (tmp_path / "database").write_text("postgresql+asyncpg://u:p@localhost/test")
    (tmp_path / "wrapped").write_text("first-wrapped-value")
    (tmp_path / "commontrace.yaml").write_text(
        "profile: server\nsettings:\n  server:\n    kms_key_id: yaml-key-id\n"
        "    kms_region: eu-west-1\nsecrets:\n  HUB_DATABASE_URL: file:database\n"
        "  HUB_ENCRYPTION_KEY_KMS_WRAPPED: file:wrapped\n"
    )
    if override:
        monkeypatch.setenv("HUB_KMS_KEY_ID", "environment-key-id")
        monkeypatch.setenv("HUB_KMS_REGION", "us-east-1")
    calls = []

    def unwrap(wrapped, *, key_id, region):
        calls.append((wrapped, key_id, region))
        return base64.urlsafe_b64encode(b"a" * 32).decode()

    monkeypatch.setattr(kms, "unwrap", unwrap)
    with apply(load(str(tmp_path))):
        assert HubConfig.from_env().cipher().enabled
        (tmp_path / "wrapped").write_text("rotated-wrapped-value")
        assert HubConfig.from_env().cipher().enabled
    assert calls == [
        (value, "environment-key-id" if override else "yaml-key-id", "us-east-1" if override else "eu-west-1")
        for value in ("first-wrapped-value", "rotated-wrapped-value")
    ]
    assert "HUB_DATABASE_URL" not in os.environ


def test_migration_keeps_file_over_plaintext_precedence_after_environment_removal(tmp_path, monkeypatch):
    import json

    from commontrace.configuration import SETTING_PATHS, migrate_environment
    from commontrace.secrets_provider import env_secret

    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    key = tmp_path / "key"
    key.write_text("mounted-secret")
    monkeypatch.setenv("COMMONTRACE_APPROVAL_KEY", "stale-plaintext-never-serialize")
    monkeypatch.setenv("COMMONTRACE_APPROVAL_KEY_FILE", str(key))
    result = migrate_environment(str(tmp_path), write=True)
    source = (tmp_path / "commontrace.yaml").read_text()
    assert "stale-plaintext-never-serialize" not in source + json.dumps(result)
    monkeypatch.delenv("COMMONTRACE_APPROVAL_KEY")
    monkeypatch.delenv("COMMONTRACE_APPROVAL_KEY_FILE")
    with apply(load(str(tmp_path))):
        assert env_secret("COMMONTRACE_APPROVAL_KEY") == "mounted-secret"
        key.write_text("rotated-secret")
        assert env_secret("COMMONTRACE_APPROVAL_KEY") == "rotated-secret"


def test_migration_uses_configured_file_and_preserves_existing_yaml(tmp_path, monkeypatch):
    from commontrace.configuration import SETTING_PATHS, migrate_environment

    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "custom.yaml"
    target.write_text("settings:\n  llm:\n    max_tokens: 71\n")
    monkeypatch.setenv("COMMONTRACE_CONFIG", str(target))
    monkeypatch.setenv("COMMONTRACE_LLM_MAX_TOKENS", "240")
    monkeypatch.setenv("COMMONTRACE_LLM_MODEL", "selected-model")
    result = migrate_environment(str(tmp_path), write=True)
    assert result["configuration"] == str(target)
    assert not (tmp_path / "commontrace.yaml").exists()
    monkeypatch.delenv("COMMONTRACE_LLM_MAX_TOKENS")
    monkeypatch.delenv("COMMONTRACE_LLM_MODEL")
    with apply(load(str(tmp_path))):
        from commontrace.configuration import getenv

        assert getenv("COMMONTRACE_LLM_MAX_TOKENS") == "71"
        assert getenv("COMMONTRACE_LLM_MODEL") == "selected-model"


def test_migration_rejects_oversized_input_before_replacing_it(tmp_path, monkeypatch):
    from commontrace.configuration import SETTING_PATHS, migrate_environment

    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "commontrace.yaml"
    source = "#" + "x" * 65536 + "\nsettings:\n  llm:\n    max_tokens: 71\n"
    target.write_text(source)
    with pytest.raises(ValueError, match="64 KiB"):
        migrate_environment(str(tmp_path), write=True)
    assert target.read_text() == source


def test_migration_does_not_override_explicit_selected_profile_options(tmp_path, monkeypatch):
    from commontrace.configuration import SETTING_PATHS, getenv, migrate_environment

    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "commontrace.yaml").write_text(
        "profile: team\nprofiles:\n  team:\n    embedding: arctic-m\n"
        "    llm_model: explicit-profile-model\n    settings:\n      llm:\n        max_tokens: 71\n"
    )
    monkeypatch.setenv("COMMONTRACE_FACT_EMBEDDER", "bge-small")
    monkeypatch.setenv("COMMONTRACE_LLM_MODEL", "ambient-model")
    monkeypatch.setenv("COMMONTRACE_LLM_MAX_TOKENS", "240")
    migrate_environment(str(tmp_path), write=True)
    for name in ("COMMONTRACE_FACT_EMBEDDER", "COMMONTRACE_LLM_MODEL", "COMMONTRACE_LLM_MAX_TOKENS"):
        monkeypatch.delenv(name)
    with apply(load(str(tmp_path))):
        assert getenv("COMMONTRACE_FACT_EMBEDDER") == "arctic-m"
        assert getenv("COMMONTRACE_LLM_MODEL") == "explicit-profile-model"
        assert getenv("COMMONTRACE_LLM_MAX_TOKENS") == "71"


def test_auth_and_hub_config_consumers_do_not_read_a_second_environment_source():
    """A second environment read bypasses request-local YAML and profile isolation."""
    import ast
    from pathlib import Path

    from commontrace import oauth
    from hub import config

    for module in (oauth, config):
        tree = ast.parse(Path(module.__file__).read_text())
        bypasses = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in ("environ", "getenv")
        ]
        assert not bypasses, f"{module.__name__} bypasses resolved configuration at {bypasses}"


def test_empty_mounted_file_selector_migrates_the_runtime_plaintext_reference(tmp_path, monkeypatch):
    from commontrace.configuration import SETTING_PATHS, migrate_environment
    from commontrace.secrets_provider import env_secret

    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMONTRACE_APPROVAL_KEY", "runtime-key")
    monkeypatch.setenv("COMMONTRACE_APPROVAL_KEY_FILE", "")
    result = migrate_environment(str(tmp_path), write=True)
    assert result["migrated_names"] == ["COMMONTRACE_APPROVAL_KEY"]
    with apply(load(str(tmp_path))):
        assert env_secret("COMMONTRACE_APPROVAL_KEY") == "runtime-key"
    assert "runtime-key" not in (tmp_path / "commontrace.yaml").read_text()


def test_migration_preserves_explicit_secret_references_and_reports_only_retained_names(tmp_path, monkeypatch):
    from commontrace.configuration import SETTING_PATHS, migrate_environment
    from commontrace.secrets_provider import env_secret

    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "key").write_text("yaml-selected-key")
    (tmp_path / "commontrace.yaml").write_text("secrets:\n  COMMONTRACE_APPROVAL_KEY: file:key\n")
    monkeypatch.setenv("COMMONTRACE_APPROVAL_KEY", "ambient-key")
    result = migrate_environment(str(tmp_path), write=True)
    assert result["retained_names"] == ["COMMONTRACE_APPROVAL_KEY"]
    assert result["migrated_names"] == []
    monkeypatch.delenv("COMMONTRACE_APPROVAL_KEY")
    with apply(load(str(tmp_path))):
        assert env_secret("COMMONTRACE_APPROVAL_KEY") == "yaml-selected-key"


def test_configured_migration_destination_cannot_escape_store_boundary(tmp_path, monkeypatch):
    from commontrace.configuration import SETTING_PATHS, migrate_environment
    from commontrace.paths import PathTraversalError

    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "outside.yaml"
    source = "settings:\n  llm:\n    max_tokens: 71\n"
    outside.write_text(source)
    monkeypatch.setenv("COMMONTRACE_CONFIG", str(outside))
    monkeypatch.setenv("COMMONTRACE_LLM_MODEL", "ambient-model")
    assert migrate_environment(str(root))["configuration"] == str(outside)
    with pytest.raises(PathTraversalError):
        migrate_environment(str(root), write=True)
    assert outside.read_text() == source


@pytest.mark.parametrize("migrate", [False, True])
def test_configuration_input_bound_counts_utf8_bytes(tmp_path, monkeypatch, migrate):
    from commontrace.configuration import SETTING_PATHS, migrate_environment

    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "commontrace.yaml"
    source = "#" + "é" * 32768 + "\n"
    assert len(source) < 65536 < len(source.encode("utf-8"))
    target.write_text(source)
    with pytest.raises(ValueError, match="64 KiB"):
        migrate_environment(str(tmp_path), write=True) if migrate else load(str(tmp_path))
    assert target.read_text() == source


def test_gateway_oauth_reads_its_own_yaml_without_an_active_configuration(tmp_path, monkeypatch):
    import json
    from concurrent.futures import ThreadPoolExecutor

    from commontrace.configuration import SETTING_PATHS
    from commontrace.gateway import Gateway

    monkeypatch.delenv("COMMONTRACE_CONFIG", raising=False)
    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    gateways = []
    for tenant in ("one", "two"):
        root = tmp_path / tenant
        root.mkdir()
        (root / "commontrace.yaml").write_text(
            "settings:\n  runtime:\n    oauth_issuer: https://" + tenant + ".issuer.example/\n"
            "    oauth_audience: https://" + tenant + ".memory.example/mcp\n"
            "    oauth_jwks_url: https://" + tenant + ".issuer.example/keys\n"
        )
        gateways.append(Gateway(str(root), durable=False))

    def request(gateway):
        metadata = gateway.handle("GET", "/.well-known/oauth-protected-resource/mcp")
        challenge = gateway.handle("GET", "/v1/status")
        return metadata, challenge

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(request, gateways))
    for tenant, (metadata, challenge) in zip(("one", "two"), results):
        assert metadata.status == 200
        assert json.loads(metadata.body)["authorization_servers"] == ["https://" + tenant + ".issuer.example/"]
        assert challenge.status == 401
        assert "https://" + tenant + ".memory.example/" in challenge.headers["WWW-Authenticate"]


def test_migration_captures_file_only_credentials_without_a_plaintext_variable(tmp_path, monkeypatch):
    from commontrace.configuration import SETTING_PATHS, migrate_environment
    from commontrace.secrets_provider import env_secret

    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    key = tmp_path / "llm-key"
    key.write_text("mounted-provider-key")
    monkeypatch.setenv("COMMONTRACE_LLM_API_KEY_FILE", str(key))
    result = migrate_environment(str(tmp_path), write=True)
    assert result["migrated_names"] == ["COMMONTRACE_LLM_API_KEY_FILE"]
    monkeypatch.delenv("COMMONTRACE_LLM_API_KEY_FILE")
    with apply(load(str(tmp_path))):
        assert env_secret("COMMONTRACE_LLM_API_KEY") == "mounted-provider-key"
    assert "mounted-provider-key" not in (tmp_path / "commontrace.yaml").read_text()


@pytest.mark.parametrize("custom", [False, True])
def test_cli_binds_selected_configuration_and_relative_secret_to_explicit_destination(tmp_path, monkeypatch, custom):
    from types import SimpleNamespace

    from commontrace import cli
    from commontrace.configuration import SETTING_PATHS, current
    from commontrace.secrets_provider import env_secret

    monkeypatch.delenv("COMMONTRACE_CONFIG", raising=False)
    for name in SETTING_PATHS:
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "explicit-destination"
    root.mkdir()
    ambient = tmp_path / "ambient-destination"
    ambient.mkdir()
    monkeypatch.setenv("COMMONTRACE_ROOT", str(ambient))
    (root / "key").write_text("destination-provider-key")
    filename = root / ("selected.yaml" if custom else "commontrace.yaml")
    filename.write_text("secrets:\n  COMMONTRACE_LLM_API_KEY: file:key\n")
    if custom:
        monkeypatch.setenv("COMMONTRACE_CONFIG", str(filename))

    def operation(_args):
        return current()["root"], env_secret("COMMONTRACE_LLM_API_KEY")

    args = SimpleNamespace(dest=str(root), command="doctor", func=operation)
    assert cli._configured_call(args) == (str(root), "destination-provider-key")
