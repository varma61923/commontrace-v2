"""CI path filter: only a diff known to touch nothing but documentation skips heavy jobs."""
import subprocess

import yaml

from scripts import ci_changes


def test_classification_is_conservative():
    assert ci_changes.code_changed(["docs/guide.md", "README.md", "LICENSE"]) is False
    assert ci_changes.code_changed(["docs/guide.md", "commontrace/cli.py"]) is True
    assert ci_changes.code_changed(["hub/README.md"]) is True  # nested docs ship with code
    assert ci_changes.code_changed(["docs/benchmarks/data.json"]) is False
    assert ci_changes.code_changed([]) is True and ci_changes.code_changed(None) is True


def test_unknown_history_never_skips(tmp_path, monkeypatch):
    assert ci_changes.changed_files("", "HEAD") is None
    assert ci_changes.changed_files(ci_changes.NULL_SHA, "HEAD") is None
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    assert ci_changes.changed_files("deadbeef" * 5, "HEAD") is None


def test_core_gates_always_run_and_heavy_jobs_wait_on_the_filter():
    jobs = yaml.safe_load(open(".github/workflows/ci.yml"))["jobs"]
    for always in ("test-core", "test-dev", "test-hub", "coverage", "security-scan", "deploy-assets"):
        assert "if" not in jobs[always] and "needs" not in jobs[always]
    gated = [name for name, job in jobs.items() if job.get("needs") == "changes"]
    assert "docker-build" in gated and "generated-sdks" in gated
    assert all(jobs[name]["if"] == "needs.changes.outputs.code == 'true'" for name in gated)
