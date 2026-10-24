"""Protect replayable history and exercise the template through Alembic itself."""
from __future__ import annotations

import ast
import hashlib
import io
import json
import shutil
from pathlib import Path
from typing import get_type_hints

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from hub import migrate

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic"


def test_reviewed_history_is_preserved_and_new_revisions_only_extend_it():
    snapshot = json.loads((MIGRATIONS / "history.json").read_text(encoding="utf-8"))
    scripts = ScriptDirectory.from_config(migrate.alembic_config())
    assert scripts.get_bases() == [snapshot["base"]]
    assert len(scripts.get_heads()) == 1
    history = list(reversed(list(scripts.walk_revisions())))
    assert len(history) >= len(snapshot["revisions"])
    assert history[len(snapshot["revisions"]) - 1].revision == snapshot["cutoff"]
    previous = None
    for revision in history:
        assert revision.down_revision == previous, "migration operator requires a linear history"
        previous = revision.revision
    for expected, actual in zip(snapshot["revisions"], history):
        assert (actual.revision, actual.down_revision, Path(actual.path).name) == (
            expected["revision"], expected["parent"], expected["file"])
        # Ignore checkout line-ending conversion; freeze the actual source text.
        source = Path(actual.path).read_text(encoding="utf-8").encode("utf-8")
        assert hashlib.sha256(source).hexdigest() == expected["sha256"], (
            f"historical revision {actual.revision} changed; add a forward migration instead")


def test_revision_replay_does_not_depend_on_mutable_application_modules():
    for path in (MIGRATIONS / "versions").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{path.name}: relative migration helper needs explicit review"
                names = [node.module or ""]
            assert not any(name.split(".")[0] in {"hub", "unimem"} for name in names), (
                f"{path.name}: freeze migration operations independently of runtime code")


def test_template_generates_loadable_revisions_and_sequence_parent_metadata(tmp_path):
    (tmp_path / "versions").mkdir()
    shutil.copyfile(MIGRATIONS / "script.py.mako", tmp_path / "script.py.mako")
    cfg = Config(stdout=io.StringIO())
    cfg.set_main_option("script_location", str(tmp_path))
    root = command.revision(cfg, message="template root", rev_id="template_root")
    left = command.revision(cfg, message="left", head=root.revision, rev_id="template_left")
    right = command.revision(cfg, message="right", head=root.revision, splice=True, rev_id="template_right")
    merged = command.merge(cfg, (left.revision, right.revision), rev_id="template_merge")
    # Rendering sequence metadata must work even though the Hub's production
    # operator deliberately supports only a linear history.
    assert merged.down_revision == (left.revision, right.revision)
    assert left.down_revision == root.revision
    for revision in (root, left, right, merged):
        get_type_hints(revision.module)
        assert revision.module.upgrade() is None
        assert revision.module.downgrade() is None
    assert ScriptDirectory.from_config(cfg).get_heads() == [merged.revision]
