import os

import pytest
import yaml

from commontrace import frontmatter, paths
from commontrace.cli import main
from commontrace.commands import dream_cmd

REFUND = "customer confused about refund timeline contradictory docs"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    for n in (1, 2, 3):
        main(["capture", "--title", f"Refund confusion {n}", "--context", REFUND, "--solution", "link the policy",
              "--tags", "refunds", "--agent-type", "support", "--not-resolved", "--dest", str(tmp_path)])
    return tmp_path


def _statuses(store):
    ldir = paths.lessons_dir(str(store))
    return {f: frontmatter.read(os.path.join(ldir, f))[0].get("status")
            for f in os.listdir(ldir) if f.startswith("lesson_") and "template" not in f}


def test_without_a_model_it_reports_and_writes_no_lessons(store, capsys):
    assert main(["dream", "--dest", str(store)]) == 0
    out = capsys.readouterr().out
    assert "1 signal(s)" in out and "report only" in out
    report = open(os.path.join(paths.memory_dir(str(store)), "dream",
                               next(iter(os.listdir(os.path.join(paths.memory_dir(str(store)), "dream"))))),
                  encoding="utf-8").read()
    assert "Failure signals (1)" in report and "No model configured" in report
    assert _statuses(store) == {}


def test_with_a_model_it_drafts_from_the_signal_and_everything_stays_in_review(store, monkeypatch, capsys):
    import json

    from commontrace import llm
    monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")
    monkeypatch.setattr(llm, "_call_anthropic", lambda cfg, prompt: (json.dumps({
        "rule": "Link the single refund policy page.", "applies_when": "A customer asks about refund timing.",
        "do_not_apply_when": "The refund was already issued.", "evidence": []}), {}))
    assert main(["dream", "--dest", str(store)]) == 0
    assert "1 new draft(s)" in capsys.readouterr().out
    statuses = _statuses(store)
    assert len(statuses) == 1 and set(statuses.values()) == {"review"}


def test_no_draft_flag_never_calls_a_model_even_when_one_is_configured(store, monkeypatch):
    from commontrace import llm
    monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")
    monkeypatch.setattr(llm, "_call_anthropic", lambda *a: (_ for _ in ()).throw(AssertionError("called")))
    assert main(["dream", "--no-draft", "--dest", str(store)]) == 0
    assert _statuses(store) == {}


def test_an_empty_store_is_a_clean_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    assert main(["dream", "--dest", str(tmp_path)]) == 0
    assert "0 signal(s)" in capsys.readouterr().out


@pytest.mark.parametrize("kind", dream_cmd.RECIPES)
def test_each_recipe_names_the_command_and_installs_nothing(kind, capsys, tmp_path):
    before = set(os.listdir(tmp_path))
    assert main(["dream", "--recipe", kind, "--every", "daily", "--dest", "memory-root"]) == 0
    text = capsys.readouterr().out
    assert "commontrace dream --dest memory-root" in text and set(os.listdir(tmp_path)) == before


def test_the_github_actions_recipe_is_valid_yaml_with_least_privilege():
    workflow = yaml.safe_load(dream_cmd.recipe("github-actions", "weekly", None).split("\n", 1)[1])
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow[True]["schedule"][0]["cron"] == "17 3 * * 1"
    assert "pull_request" not in workflow[True]
