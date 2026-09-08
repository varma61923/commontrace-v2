"""commontrace/paths.py -- root resolution and the implicit-cwd-store warning."""
from __future__ import annotations

import os

from commontrace import paths


class TestWarnIfImplicitCwdStoreMatchesResolveRootTruthiness:
    """`resolve_root` treats an empty string the same as no `--dest` at all
    (`if explicit:`), so the warning that fires on the fallback path must
    agree -- a mismatch here silently suppresses the warning exactly when a
    write is about to materialize a store somewhere unexpected."""

    def test_an_empty_explicit_falls_through_like_none(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
        monkeypatch.delenv("JUSTDOIT_ROOT", raising=False)

        resolved_empty = paths.resolve_root("")
        resolved_none = paths.resolve_root(None)
        assert resolved_empty == resolved_none

        paths.warn_if_implicit_cwd_store("")
        warned_for_empty = "no store found" in capsys.readouterr().err

        paths.warn_if_implicit_cwd_store(None)
        warned_for_none = "no store found" in capsys.readouterr().err

        assert warned_for_empty == warned_for_none is True

    def test_a_real_explicit_dest_never_warns(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        paths.warn_if_implicit_cwd_store(str(tmp_path / "somewhere"))
        assert capsys.readouterr().err == ""

    def test_an_existing_store_in_cwd_does_not_warn(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
        monkeypatch.delenv("JUSTDOIT_ROOT", raising=False)
        os.makedirs(os.path.join(str(tmp_path), "memory"), exist_ok=True)

        paths.warn_if_implicit_cwd_store(None)
        assert capsys.readouterr().err == ""
