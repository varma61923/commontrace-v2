"""Skills discovery + watcher + consolidate emission + agent-loop injection."""
from __future__ import annotations

import os

import pytest

from commontrace import consolidate, frontmatter, skills, templates


def _write_skill(skills_dir: str, name: str, description: str, body: str = "Do the thing.",
                 extra: dict | None = None, flat: bool = False) -> str:
    fm: dict = {"name": name, "description": description}
    if extra:
        fm.update(extra)
    if flat:
        os.makedirs(skills_dir, exist_ok=True)
        path = os.path.join(skills_dir, f"{name}.md")
    else:
        dir_path = os.path.join(skills_dir, name)
        os.makedirs(dir_path, exist_ok=True)
        path = os.path.join(dir_path, "SKILL.md")
    frontmatter.write(path, fm, body)
    return path


def _write_raw(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


@pytest.fixture()
def isolated_user(tmp_path, monkeypatch):
    """An empty user skills dir so HOME leakage cannot affect tests."""
    user_dir = str(tmp_path / "user-skills")
    os.makedirs(user_dir, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    return user_dir


class TestFrontmatterValidation:
    def test_valid_frontmatter_has_no_errors(self):
        assert skills.validate_skill_frontmatter(
            {"name": "my-skill", "description": "Does X. Use when Y."}, "p") == []

    def test_optional_fields_are_accepted(self):
        fm = {"name": "s", "description": "d",
              "when_to_use": "when the task matches", "user_invocable": True}
        assert skills.validate_skill_frontmatter(fm, "p") == []

    def test_missing_name_is_rejected(self):
        errors = skills.validate_skill_frontmatter({"description": "d"}, "p")
        assert any("name" in e for e in errors)

    def test_missing_description_is_rejected(self):
        errors = skills.validate_skill_frontmatter({"name": "s"}, "p")
        assert any("description" in e for e in errors)

    def test_non_boolean_user_invocable_is_rejected(self):
        errors = skills.validate_skill_frontmatter(
            {"name": "s", "description": "d", "user_invocable": "yes"}, "p")
        assert any("user_invocable" in e for e in errors)

    def test_non_string_when_to_use_is_rejected(self):
        errors = skills.validate_skill_frontmatter(
            {"name": "s", "description": "d", "when_to_use": 42}, "p")
        assert any("when_to_use" in e for e in errors)

    def test_bad_slug_is_rejected(self):
        errors = skills.validate_skill_frontmatter(
            {"name": "Not A Slug!", "description": "d"}, "p")
        assert any("name" in e for e in errors)


class TestDiscoveryOrdering:
    def test_project_skill_is_discovered(self, tmp_path, isolated_user):
        root = str(tmp_path / "root")
        _write_skill(os.path.join(root, "skills"), "deploy-check",
                     "Check deploys. Use when releasing.")
        found = skills.discover(root, user_dir=isolated_user, include_bundled=False)
        assert [s.name for s in found] == ["deploy-check"]
        assert found[0].source == "project"

    def test_flat_skill_files_are_discovered(self, tmp_path, isolated_user):
        root = str(tmp_path / "root")
        _write_skill(os.path.join(root, "skills"), "flat-one",
                     "Flat skill.", flat=True)
        found = skills.discover(root, user_dir=isolated_user, include_bundled=False)
        assert any(s.name == "flat-one" for s in found)

    def test_invalid_frontmatter_files_are_rejected(self, tmp_path, isolated_user):
        root = str(tmp_path / "root")
        sdir = os.path.join(root, "skills")
        _write_skill(sdir, "good-skill", "Good. Use when testing.")
        bad_dir = os.path.join(sdir, "bad-skill")
        os.makedirs(bad_dir, exist_ok=True)
        # Missing description -> rejected.
        _write_raw(os.path.join(bad_dir, "SKILL.md"),
                   "---\nname: bad-skill\n---\n\nBody.\n")
        wrong_type_dir = os.path.join(sdir, "wrong-type")
        os.makedirs(wrong_type_dir, exist_ok=True)
        _write_raw(os.path.join(wrong_type_dir, "SKILL.md"),
                   "---\nname: wrong-type\ndescription: d\nuser_invocable: yes\n---\n\nBody.\n")
        found = skills.discover(root, user_dir=isolated_user, include_bundled=False)
        names = [s.name for s in found]
        assert "good-skill" in names
        assert "bad-skill" not in names
        assert "wrong-type" not in names

    def test_project_overrides_bundled_on_name_collision(self, tmp_path, isolated_user):
        from commontrace import kb_packs

        packs = kb_packs.list_packs()
        assert packs, "expected bundled kb_packs for the ordering test"
        bundled_name = sorted(p.name for p in packs)[0]
        root = str(tmp_path / "root")
        _write_skill(os.path.join(root, "skills"), bundled_name,
                     "Project override description. Use when testing overrides.")
        found = skills.discover(root, user_dir=isolated_user, include_bundled=True)
        matches = [s for s in found if s.name == bundled_name]
        assert len(matches) == 1
        assert matches[0].source == "project"
        assert "override" in matches[0].description.lower()
        # Project skills come before bundled ones in priority order.
        first_bundled = next((i for i, s in enumerate(found) if s.source == "bundled"), None)
        if first_bundled is not None:
            assert found.index(matches[0]) < first_bundled

    def test_user_skills_slot_between_project_and_bundled(self, tmp_path):
        root = str(tmp_path / "root")
        user_dir = str(tmp_path / "user")
        os.makedirs(user_dir, exist_ok=True)
        _write_skill(os.path.join(root, "skills"), "aaa-project", "Project skill.")
        _write_skill(user_dir, "mmm-user", "User skill.")
        found = skills.discover(root, user_dir=user_dir, include_bundled=False)
        assert [s.name for s in found] == ["aaa-project", "mmm-user"]


class TestWatcherReload:
    def test_poll_detects_a_new_skill_file(self, tmp_path, isolated_user):
        root = str(tmp_path / "root")
        os.makedirs(os.path.join(root, "skills"), exist_ok=True)
        watcher = skills.SkillWatcher(root, user_dir=isolated_user, include_bundled=False)
        assert watcher.skills == []
        changed, _ = watcher.poll()
        assert changed is False

        _write_skill(os.path.join(root, "skills"), "fresh-skill",
                     "Fresh. Use when testing the watcher.")
        changed, current = watcher.poll()
        assert changed is True
        assert [s.name for s in current] == ["fresh-skill"]
        # Second poll with no changes reports clean.
        changed, _ = watcher.poll()
        assert changed is False

    def test_poll_detects_modification_and_deletion(self, tmp_path, isolated_user):
        root = str(tmp_path / "root")
        path = _write_skill(os.path.join(root, "skills"), "mutable",
                            "Version one.")
        watcher = skills.SkillWatcher(root, user_dir=isolated_user, include_bundled=False)
        assert watcher.get("mutable") is not None
        assert watcher.get("mutable").description == "Version one."

        frontmatter.write(path, {"name": "mutable", "description": "Version two."},
                          "Updated body.")
        assert watcher.refresh_if_changed() is True
        assert watcher.get("mutable").description == "Version two."

        os.unlink(path)
        try:
            os.rmdir(os.path.dirname(path))
        except OSError:
            pass
        assert watcher.refresh_if_changed() is True
        assert watcher.get("mutable") is None


SAME_RULE = "Never retry a payment without an idempotency key on the write path."


def _lesson(slug, description, applies_when, body, *, status="active"):
    return {
        "name": slug, "status": status, "description": description,
        "applies_when": applies_when, "do_not_apply_when": "n/a",
        "tags": [], "domain": "other", "uses": 0, "last_hit": "NEVER",
        templates.BODY_KEY: body,
    }


class TestEmissionTrigger:
    def test_contradiction_free_fuse_pair_emits_a_skill_draft(self, tmp_path):
        root = str(tmp_path / "root")
        lessons = [
            _lesson("first", "Payment webhook delivered more than once.",
                    "A webhook is retried after a timeout.", SAME_RULE),
            _lesson("second", "Duplicate charge from a retried webhook.",
                    "A webhook is retried after a timeout.", SAME_RULE),
        ]
        report = consolidate.build_report(lessons)
        assert len(report.fuse) == 1
        assert report.contradict == ()

        candidates = consolidate.find_skill_candidates(report, lessons)
        assert len(candidates) == 1
        assert set(candidates[0].sources) == {"first", "second"}

        written = consolidate.emit_skill_drafts(root, candidates)
        assert len(written) == 1
        skill_file = written[0]
        assert skill_file.startswith(os.path.join(os.path.abspath(root), "skills"))
        assert os.path.basename(skill_file) == "SKILL.md"
        fm, body = frontmatter.read(skill_file)
        assert skills.validate_skill_frontmatter(fm, skill_file) == []
        assert fm["name"] == candidates[0].name
        assert "first" in body and "second" in body

        # The emitted draft is discoverable as a project skill.
        found = skills.discover(root, user_dir=str(tmp_path / "nouser"),
                                include_bundled=False)
        assert any(s.name == candidates[0].name for s in found)

    def test_contradicted_pair_does_not_emit(self, tmp_path):

        lessons = [
            _lesson("always", "Always retry a failed webhook delivery",
                    "a webhook delivery fails transiently", "body"),
            _lesson("never", "Never retry a failed webhook delivery",
                    "a webhook delivery fails transiently", "body"),
        ]
        report = consolidate.build_report(lessons)
        assert len(report.contradict) == 1
        # Force a fuse pair over the contradicted slugs even if the
        # lexical gate would not fire, to isolate the emission rule.
        fused = consolidate.ConsolidationReport(
            n_active=2,
            fuse=(rel_pair("always", "never"),),
            contradict=tuple(report.contradict),
        )
        candidates = consolidate.find_skill_candidates(fused, lessons)
        assert candidates == []
        assert consolidate.emit_skill_drafts(str(tmp_path / "root"), candidates) == []

    def test_existing_draft_is_left_alone_without_overwrite(self, tmp_path):
        root = str(tmp_path / "root")
        lessons = [
            _lesson("first", "x", "y", SAME_RULE),
            _lesson("second", "x", "y", SAME_RULE),
        ]
        report = consolidate.build_report(lessons)
        candidates = consolidate.find_skill_candidates(report, lessons)
        assert len(candidates) == 1
        first_write = consolidate.emit_skill_drafts(root, candidates)
        assert len(first_write) == 1
        assert consolidate.emit_skill_drafts(root, candidates) == []
        rewritten = consolidate.emit_skill_drafts(root, candidates, overwrite=True)
        assert len(rewritten) == 1


def rel_pair(a: str, b: str):
    from commontrace.redundancy import Pair

    return Pair.of(a, b, 0.9)


class TestAgentLoopInjection:
    def test_context_lists_skill_names_without_bodies(self, tmp_path):
        from commontrace.agent_loop import _assemble_context

        root = str(tmp_path / "root")
        os.makedirs(os.path.join(root, "memory", "lessons"), exist_ok=True)
        secret_body = "UNIQUE-BODY-MARKER-987654321"
        _write_skill(os.path.join(root, "skills"), "pay-check",
                     "Check payments. Use when handling webhooks.", body=secret_body)
        ctx = _assemble_context(root, "Handle a webhook redelivery")
        assert "pay-check" in ctx
        assert "Check payments" in ctx
        assert secret_body not in ctx

    def test_context_without_skills_still_assembles(self, tmp_path):
        from commontrace.agent_loop import _assemble_context

        root = str(tmp_path / "empty")
        os.makedirs(os.path.join(root, "memory", "lessons"), exist_ok=True)
        ctx = _assemble_context(root, "Do something")
        assert "Do something" in ctx

    def test_skill_body_loads_on_demand(self, tmp_path, isolated_user):
        root = str(tmp_path / "root")
        body = "Step one. Step two."
        _write_skill(os.path.join(root, "skills"), "on-demand",
                     "On demand skill.", body=body)
        skill = skills.get_skill(root, "on-demand", user_dir=isolated_user,
                                 include_bundled=False)
        assert skill is not None
        assert body in skills.load_body(skill)
