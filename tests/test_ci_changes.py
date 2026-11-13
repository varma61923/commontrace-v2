"""CI path filter: only a diff known to touch nothing but documentation skips heavy jobs."""

import subprocess
from pathlib import Path

import yaml

from scripts import ci_changes


def test_code_moved_into_docs_still_runs_code_gates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    source = Path("unimem/example.py")
    source.parent.mkdir()
    source.write_text("def example():\n    return 42\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "source"], check=True
    )
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    Path("docs").mkdir()
    subprocess.run(["git", "mv", str(source), "docs/example.md"], check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "move"], check=True
    )
    changed = ci_changes.changed_files(base, "HEAD")
    assert changed is not None and "unimem/example.py" in changed
    assert ci_changes.code_changed(changed)


def test_codeql_actions_share_one_immutable_revision():
    jobs = yaml.safe_load(Path(".github/workflows/security.yml").read_text())["jobs"]
    actions = [
        step["uses"]
        for job in jobs.values()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("github/codeql-action/")
    ]
    assert len(actions) >= 2
    revisions = {action.split("@", 1)[1] for action in actions}
    assert len(revisions) == 1, "CodeQL init and analyze cannot exchange state across versions"
    assert all(len(revision) == 40 and all(c in "0123456789abcdef" for c in revision) for revision in revisions)


def test_dependabot_groups_codeql_action_updates():
    updates = yaml.safe_load(Path(".github/dependabot.yml").read_text())["updates"]
    actions = next(update for update in updates if update["package-ecosystem"] == "github-actions")
    assert any("github/codeql-action/*" in group.get("patterns", []) for group in actions.get("groups", {}).values())


def test_classification_is_conservative():
    assert ci_changes.code_changed(["docs/guide.md", "README.md", "LICENSE"]) is False
    assert ci_changes.code_changed(["docs/guide.md", "unimem/cli.py"]) is True
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
    jobs = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())["jobs"]
    for always in ("test-core", "test-dev", "test-hub", "security-scan", "deploy-assets"):
        assert "if" not in jobs[always] and "needs" not in jobs[always]
    gated = [name for name, job in jobs.items() if job.get("needs") == "changes"]
    assert "docker-build" in gated and "sdk-typescript" in gated
    assert all(jobs[name]["if"] == "needs.changes.outputs.code == 'true'" for name in gated)


def test_each_commit_runs_once_and_the_suite_runs_in_parallel():
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())
    triggers = workflow.get(True, workflow.get("on"))
    assert triggers["push"] == {"branches": ["main"]}  # branch commits run through their PR only
    jobs = workflow["jobs"]
    for name in ("test-core", "test-dev", "test-hub"):
        steps = " ".join(str(step.get("run", "")) for step in jobs[name]["steps"])
        assert "-n auto" in steps
    assert "--cov-fail-under=80" in " ".join(str(s.get("run", "")) for s in jobs["test-dev"]["steps"])
    assert "coverage" not in jobs and "generated-sdks" not in jobs


def test_browser_ci_requires_chromium_and_runs_both_console_suites():
    job = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())["jobs"]["test-browser"]
    assert job["env"]["UNIMEM_REQUIRE_BROWSER"] == "1"
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "playwright install --with-deps chromium" in commands
    assert "tests/test_ui_browser.py" in commands
    assert "hub/tests/test_console_accessibility.py" in commands
