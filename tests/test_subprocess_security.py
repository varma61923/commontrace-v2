"""Tests for subprocess execution security, PYTHONSAFEPATH hardening, and shadow module immunity."""
import os
import subprocess
import sys

from commontrace.commands._shellout import (
    _store_scripts_allowed,
    find_reference_script,
    packaged_reference_dir,
    run_script,
)


class TestSubprocessHardening:
    def test_run_script_sets_pythonsafepath_and_utf8(self, tmp_path, monkeypatch):
        """Verify run_script injects PYTHONSAFEPATH=1, PYTHONUTF8=1, and COMMONTRACE_ROOT."""
        recorded_calls = []

        def mock_subprocess_run(cmd, **kwargs):
            recorded_calls.append((cmd, kwargs))
            class DummyResult:
                returncode = 0
                stdout = "output"
            return DummyResult()

        monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

        root = str(tmp_path)
        rc, out = run_script(
            root,
            "measure_performance.py",
            ["--help"],
            "hint",
            capture=True,
            extra_env={"CUSTOM_KEY": "CUSTOM_VAL"},
        )

        assert rc == 0
        assert out == "output"
        assert len(recorded_calls) == 1
        cmd, kwargs = recorded_calls[0]

        env = kwargs["env"]
        assert env["COMMONTRACE_ROOT"] == root
        assert env["PYTHONUTF8"] == "1"
        assert env["PYTHONSAFEPATH"] == "1"
        assert env["CUSTOM_KEY"] == "CUSTOM_VAL"

        # Check -P flag for Python 3.11+
        if sys.version_info >= (3, 11):
            assert "-P" in cmd
            assert cmd[1] == "-P"

    def test_run_script_missing_script_fails_gracefully(self, tmp_path, capsys):
        rc = run_script(str(tmp_path), "nonexistent_script_xyz.py", [], "Please check install.", capture=False)
        captured = capsys.readouterr()
        assert rc == 1
        assert "Could not find nonexistent_script_xyz.py" in captured.err
        assert "Please check install." in captured.err

        rc, out = run_script(str(tmp_path), "nonexistent_script_xyz.py", [], "Please check install.", capture=True)
        captured = capsys.readouterr()
        assert rc == 1
        assert out == ""
        assert "Could not find nonexistent_script_xyz.py" in captured.err


class TestShadowModuleImmunity:
    def test_shadow_stdlib_module_ignored_under_run_script(self, tmp_path):
        """Ensure standard library shadowing (e.g. malicious json.py in CWD) is blocked."""
        if sys.version_info < (3, 11):
            pytest.skip("PYTHONSAFEPATH and -P require Python 3.11+")

        # Create a malicious json.py in an isolated directory
        malicious_dir = tmp_path / "untrusted_workspace"
        malicious_dir.mkdir()
        evil_json = malicious_dir / "json.py"
        evil_json.write_text("raise RuntimeError('SHADOW_JSON_HIJACKED')\n", encoding="utf-8")

        evil_argparse = malicious_dir / "argparse.py"
        evil_argparse.write_text("raise RuntimeError('SHADOW_ARGPARSE_HIJACKED')\n", encoding="utf-8")

        # Create a test script in the workspace that imports json and argparse
        target_script = malicious_dir / "test_import.py"
        target_script.write_text(
            "import json\n"
            "import argparse\n"
            "print('SUCCESS_NOT_HIJACKED')\n",
            encoding="utf-8",
        )

        old_cwd = os.getcwd()
        try:
            os.chdir(str(malicious_dir))

            # Control experiment: without PYTHONSAFEPATH and without -P, python imports CWD json.py
            control_res = subprocess.run(
                [sys.executable, str(target_script)],
                capture_output=True,
                text=True,
                cwd=str(malicious_dir),
            )
            # Control should fail due to hijacking
            assert control_res.returncode != 0
            assert "SHADOW_JSON_HIJACKED" in control_res.stderr or "SHADOW_ARGPARSE_HIJACKED" in control_res.stderr

            # Now test with run_script:
            # We enable store scripts so it runs our target_script from malicious_dir
            os.environ["COMMONTRACE_ALLOW_STORE_SCRIPTS"] = "1"
            rc, out = run_script(
                str(malicious_dir),
                "test_import.py",
                [],
                "missing",
                capture=True,
            )
            assert rc == 0
            assert "SUCCESS_NOT_HIJACKED" in out

        finally:
            os.chdir(old_cwd)
            os.environ.pop("COMMONTRACE_ALLOW_STORE_SCRIPTS", None)


class TestStoreScriptsSecurityPolicy:
    def test_store_scripts_allowed_flag(self, monkeypatch):
        monkeypatch.delenv("COMMONTRACE_ALLOW_STORE_SCRIPTS", raising=False)
        assert _store_scripts_allowed() is False

        for val in ["0", "false", "no", "off", "anything_else"]:
            monkeypatch.setenv("COMMONTRACE_ALLOW_STORE_SCRIPTS", val)
            assert _store_scripts_allowed() is False

        for val in ["1", "true", "yes", "on", "True", "YES", " 1 "]:
            monkeypatch.setenv("COMMONTRACE_ALLOW_STORE_SCRIPTS", val)
            assert _store_scripts_allowed() is True

    def test_find_reference_script_default_ignores_store_script(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COMMONTRACE_ALLOW_STORE_SCRIPTS", raising=False)

        # Create a store-root script
        store_script = tmp_path / "measure_performance.py"
        store_script.write_text("# store copy", encoding="utf-8")

        found = find_reference_script(str(tmp_path), "measure_performance.py")
        # Must return the packaged copy, NOT the store script
        assert found is not None
        assert os.path.dirname(found) == packaged_reference_dir()
        assert found != str(store_script)

    def test_find_reference_script_with_opt_in_uses_store_script(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("COMMONTRACE_ALLOW_STORE_SCRIPTS", "1")

        store_script = tmp_path / "measure_performance.py"
        store_script.write_text("# store copy", encoding="utf-8")

        found = find_reference_script(str(tmp_path), "measure_performance.py")
        captured = capsys.readouterr()

        assert found == str(store_script)
        assert "running reference script from store root" in captured.err

    def test_find_reference_script_rejects_path_traversal_even_with_opt_in(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_ALLOW_STORE_SCRIPTS", "1")

        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        evil_script = outside_dir / "evil.py"
        evil_script.write_text("# evil", encoding="utf-8")

        store_dir = tmp_path / "store"
        store_dir.mkdir()

        # Relative path attempting traversal outside store_dir
        found = find_reference_script(str(store_dir), "../outside/evil.py")
        # Must not return the traversed path
        assert found != str(evil_script)
