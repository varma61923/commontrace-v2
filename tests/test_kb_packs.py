"""commontrace/kb_packs.py and `commontrace kb`. The properties worth
protecting: a pack is a SELECTION of the already-curated corpus (not new
content), installs only at status=review, cannot be approved until a
person states where it does not apply, and never overwrites a reviewer's
edits on re-install.
"""
from __future__ import annotations

import json
import os

import pytest

from commontrace import frontmatter, kb_packs, paths
from commontrace.cli import main

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(REPO_ROOT, "commons", "seed", "substrate-v1.jsonl")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "code", "--dest", str(tmp_path)])
    return tmp_path


def _installed(store) -> list[str]:
    return sorted(f for f in os.listdir(paths.lessons_dir(str(store))) if f.startswith("lesson_kb_"))


class TestPacksAreASelectionNotNewContent:
    def test_every_pack_record_exists_verbatim_in_the_curated_corpus(self):
        with open(CORPUS, encoding="utf-8") as fh:
            corpus = {json.dumps(json.loads(line), sort_keys=True) for line in fh if line.strip()}
        for info in kb_packs.list_packs():
            with open(os.path.join(kb_packs.PACKS_DIR, f"{info.name}.jsonl"), encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        assert json.dumps(json.loads(line), sort_keys=True) in corpus, info.name

    def test_every_record_carries_a_source_citation(self):
        for info in kb_packs.list_packs():
            for record in kb_packs._read(kb_packs._pack_path(info.name)):
                assert record.get("source"), (info.name, record.get("title"))


class TestListPacks:
    def test_the_shipped_packs_are_listed_with_counts(self):
        packs = {p.name: p for p in kb_packs.list_packs()}
        assert set(packs) == {"databases", "security", "kubernetes-deployment", "concurrency-resilience"}
        assert all(p.count > 0 for p in packs.values())
        assert all(len(p.version) == 12 for p in packs.values())

    def test_cli_list(self, capsys):
        assert main(["kb", "list"]) == 0
        assert "kubernetes-deployment" in capsys.readouterr().out


class TestInstall:
    def test_installs_every_record_at_status_review(self, store):
        result = kb_packs.install_pack(str(store), "kubernetes-deployment")
        expected = next(p.count for p in kb_packs.list_packs() if p.name == "kubernetes-deployment")
        assert len(result.written) == expected
        for name in _installed(store):
            fm, _body = frontmatter.read(os.path.join(paths.lessons_dir(str(store)), name))
            assert fm["status"] == "review"
            assert fm["kb_pack"] == {"name": "kubernetes-deployment", "version": result.version}

    def test_installed_lessons_pass_schema_validation(self, store, capsys):
        kb_packs.install_pack(str(store), "databases")
        assert main(["lesson", "validate", "--dest", str(store)]) == 0

    def test_approval_refuses_an_installed_lesson_until_a_person_edits_it(self, store, capsys):
        result = kb_packs.install_pack(str(store), "security")
        capsys.readouterr()
        assert main(["lesson", "approve", result.written[0], "--dest", str(store)]) == 1
        assert "unedited scaffolding" in capsys.readouterr().err

    def test_reinstalling_skips_existing_and_keeps_edits(self, store):
        first = kb_packs.install_pack(str(store), "databases")
        path = os.path.join(paths.lessons_dir(str(store)), f"{first.written[0]}.md")
        fm, body = frontmatter.read(path)
        fm["do_not_apply_when"] = "edited by a reviewer"
        frontmatter.write(path, fm, body)

        second = kb_packs.install_pack(str(store), "databases")
        assert second.written == []
        assert sorted(second.skipped_existing) == sorted(first.written)
        assert frontmatter.read(path)[0]["do_not_apply_when"] == "edited by a reviewer"

    def test_explicit_agent_type_wins_over_the_records_own(self, store):
        result = kb_packs.install_pack(str(store), "databases", agent_type="ops")
        fm, _ = frontmatter.read(os.path.join(paths.lessons_dir(str(store)), f"{result.written[0]}.md"))
        assert fm["agent_type"] == "ops"


class TestRefusals:
    @pytest.mark.parametrize("name", ["nope", "../etc/passwd", "Databases", "", "a/b"])
    def test_unknown_or_unsafe_names_are_refused(self, name):
        with pytest.raises(kb_packs.UnknownPack):
            kb_packs._pack_path(name)

    def test_cli_unknown_pack_fails_cleanly(self, store, capsys):
        assert main(["kb", "install", "nope", "--dest", str(store)]) == 1
        assert "no such pack" in capsys.readouterr().err
