"""Tests for `commontrace doctor`'s [OK]/[INFO]/[WARN] severity classification.

Reclassification background: a fresh, normal client install (pip install -e ., then
commontrace init, run outside a repo checkout) previously showed 4-5 [WARN] lines for
conditions that are expected and require no action -- the optional attention extra not
being installed, and reference scripts / protocol/ only existing inside a source checkout.
Only a genuine problem (e.g. zero lessons captured yet) should read as [WARN]; the rest
belong at [INFO].
"""
import os

import pytest

from commontrace.cli import main


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """A store outside any repo checkout -- the normal shape for a pip-installed client,
    as opposed to running from within the commontrace-v2 source tree."""
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "code", "--dest", str(tmp_path)])
    return tmp_path


def test_fresh_client_install_has_no_actionable_warnings_beyond_empty_store(fresh_store, capsys):
    """A fresh install's only real problem is having zero lessons yet -- everything else
    (attention extra, reference scripts, protocol/ dir) is expected to be absent for a
    pip-installed client and must not read as [WARN]."""
    assert main(["doctor", "--dest", str(fresh_store)]) == 0
    out = capsys.readouterr().out

    warn_lines = [line for line in out.splitlines() if line.startswith("[WARN]")]
    info_lines = [line for line in out.splitlines() if line.startswith("[INFO]")]

    assert len(warn_lines) == 1
    assert "lessons in store" in warn_lines[0]

    # Labels are stated neutrally ("attention extra", not "attention extra
    # installed"): the INFO branch reports ABSENCE, and reusing the
    # affirmative label made it claim the opposite of its own detail.
    info_labels = {
        "attention extra (numpy + sentence-transformers)",
        "reference attention/query.py",
        "protocol/ spec",
    }
    for label in info_labels:
        assert any(label in line for line in info_lines), f"expected an [INFO] line for: {label}"

    # None of the informational conditions leaked through as [WARN].
    for label in info_labels:
        assert not any(label in line for line in warn_lines), f"{label} should not be [WARN]"

    # The benchmark script ships inside the wheel, so a client with no repo
    # checkout still gets [OK] -- `commontrace bench --pilot` works for them.
    # Absence would mean a damaged install, which is a real problem, not info.
    assert "[OK  ] benchmark script found" in out
    # [BUG-CLI-05]: doctor checked for measure_performance.py but not its
    # sibling pilot_metrics.py -- `commontrace bench --pilot`/`commontrace
    # pilot` import it directly, so a damaged install missing only this one
    # file passed doctor cleanly and then failed at runtime with a raw
    # ImportError instead of doctor's own clean diagnostic.
    assert "[OK  ] pilot metrics script found" in out


def test_doctor_still_warns_on_genuine_problems(fresh_store, capsys):
    """Reclassifying the informational checks must not silence real problems."""
    assert main(["doctor", "--dest", str(fresh_store)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] lessons in store - 0 found" in out


def test_doctor_warns_when_pilot_metrics_is_missing_from_a_damaged_install(
    fresh_store, capsys, monkeypatch
):
    """[BUG-CLI-05]: a damaged install missing only pilot_metrics.py (but
    still carrying measure_performance.py) must be caught here, not
    silently pass doctor and fail later at runtime."""
    from commontrace.commands import doctor_cmd

    real_find = doctor_cmd.find_reference_script

    def _find_missing_pilot_metrics(root, relative):
        if relative == "benchmark/pilot_metrics.py":
            return None
        return real_find(root, relative)

    monkeypatch.setattr(doctor_cmd, "find_reference_script", _find_missing_pilot_metrics)
    assert main(["doctor", "--dest", str(fresh_store)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] pilot metrics script found" in out
    assert "[OK  ] benchmark script found" in out


def test_doctor_in_repo_checkout_shows_ok_not_info_for_repo_only_checks(capsys):
    """Inside an actual repo checkout, the repo-only checks (reference scripts, protocol/)
    should still show [OK], not [INFO] -- INFO is only for the absent case."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert main(["doctor", "--dest", repo_root]) == 0
    out = capsys.readouterr().out
    assert "[OK  ] reference attention/query.py" in out
    assert "[OK  ] benchmark script found" in out
    assert "[OK  ] pilot metrics script found" in out
    assert "[OK  ] protocol/ spec" in out
