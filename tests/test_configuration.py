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

        assert os.environ["COMMONTRACE_FACT_EMBEDDER"] == "arctic-m"
        assert os.environ["COMMONTRACE_REDACT_PII"] == "1"
    (tmp_path / "commontrace.yaml").write_text("profile: unknown\n")
    with pytest.raises(ValueError):
        load(str(tmp_path))
