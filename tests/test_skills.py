from __future__ import annotations

import os

import pytest

from commontrace import frontmatter, skills


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
