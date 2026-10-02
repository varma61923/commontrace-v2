import os

import pytest

from commontrace.cli import main


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "code", "--dest", str(tmp_path)])
    return tmp_path


def test_fresh_client_install_has_no_actionable_warnings_beyond_empty_store(fresh_store, capsys):
    assert main(["doctor", "--dest", str(fresh_store)]) == 0
    out = capsys.readouterr().out

    warn_lines = [line for line in out.splitlines() if line.startswith("[WARN]")]
    info_lines = [line for line in out.splitlines() if line.startswith("[INFO]")]

    assert len(warn_lines) == 1
    assert "lessons in store" in warn_lines[0]

    info_labels = {
        "protocol/ spec",
    }
    for label in info_labels:
        assert any(label in line for line in info_lines), f"expected an [INFO] line for: {label}"

    attention_lines = [
        line for line in out.splitlines()
        if "attention extra (numpy + sentence-transformers)" in line
    ]
    assert len(attention_lines) == 1
    assert attention_lines[0].startswith(("[OK  ]", "[INFO]")), attention_lines[0]

    for label in info_labels | {"attention extra (numpy + sentence-transformers)"}:
        assert not any(label in line for line in warn_lines), f"{label} should not be [WARN]"

    assert "[OK  ] benchmark script found" in out
    assert "[OK  ] pilot metrics script found" in out


def test_doctor_still_warns_on_genuine_problems(fresh_store, capsys):
    assert main(["doctor", "--dest", str(fresh_store)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] lessons in store - 0 found" in out


def test_doctor_warns_when_pilot_metrics_is_missing_from_a_damaged_install(
    fresh_store, capsys, monkeypatch
):
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
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert main(["doctor", "--dest", repo_root]) == 0
    out = capsys.readouterr().out
    assert "[OK  ] reference attention/query.py" in out
    assert "[OK  ] benchmark script found" in out
    assert "[OK  ] pilot metrics script found" in out
    assert "[OK  ] protocol/ spec" in out


class TestDoctorReportsAgentNativeAccess:
    @staticmethod
    def _run(tmp_path, capsys):
        import argparse

        from commontrace.commands import doctor_cmd

        doctor_cmd.run(argparse.Namespace(dest=str(tmp_path)))
        return capsys.readouterr().out

    def test_it_reports_the_mcp_sdk(self, tmp_path, capsys):
        out = self._run(tmp_path, capsys)
        assert "commontrace serve" in out

    def test_its_absence_is_information_not_a_failure(self, tmp_path, capsys, monkeypatch):
        from commontrace.commands import doctor_cmd

        real = doctor_cmd._installed
        monkeypatch.setattr(doctor_cmd, "_installed",
                            lambda name: False if name == "mcp" else real(name))
        out = self._run(tmp_path, capsys)
        assert "commontrace[serve]" in out
        line = next(x for x in out.splitlines() if "commontrace serve" in x)
        assert line.startswith("[INFO]"), line


class TestAProbeCannotTakeDownTheReport:
    def test_a_raising_finder_does_not_crash_doctor(self, tmp_path, capsys, monkeypatch):
        import argparse
        import importlib.util

        from commontrace.commands import doctor_cmd

        def explode(name, *args, **kwargs):
            raise ModuleNotFoundError(f"a hostile import hook rejected {name!r}")

        monkeypatch.setattr(importlib.util, "find_spec", explode)
        monkeypatch.setattr(doctor_cmd.sys, "modules", {})
        doctor_cmd.run(argparse.Namespace(dest=str(tmp_path)))
        out = capsys.readouterr().out
        assert "store root" in out and "memory/ store present" in out

    def test_an_unanswerable_probe_reads_as_not_installed(self, monkeypatch):
        import importlib.util

        from commontrace.commands import doctor_cmd

        monkeypatch.setattr(importlib.util, "find_spec",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("hook")))
        assert doctor_cmd._installed("definitely_not_imported_anywhere") is False


def test_doctor_says_whether_the_models_a_store_uses_are_cached(fresh_store, capsys, monkeypatch):
    from commontrace import retrieval_io
    from commontrace.commands import doctor_cmd

    monkeypatch.setattr(doctor_cmd, "has_attention_deps", lambda: True, raising=False)
    retrieval_io.configure(str(fresh_store), fusion=retrieval_io.FUSION_GATED, rerank=retrieval_io.RERANK_CE)
    monkeypatch.setattr(doctor_cmd, "_model_cached", lambda name: "cross-encoder" in name)
    main(["doctor", "--dest", str(fresh_store)])
    out = capsys.readouterr().out
    if "attention extra (numpy + sentence-transformers) - installed" not in out:
        pytest.skip("the attention extra is not installed here")
    assert "[OK  ] reranker model cached - cross-encoder/ms-marco-MiniLM-L-6-v2" in out
    assert "[INFO] embedding model - Snowflake/snowflake-arctic-embed-m-v1.5 is not in the local model cache" in out


def test_a_bare_model_name_is_looked_up_where_sentence_transformers_puts_it(monkeypatch):
    import sys
    import types

    from commontrace.commands import doctor_cmd

    seen = []
    fake = types.SimpleNamespace(try_to_load_from_cache=lambda repo, filename: seen.append(repo) or None)
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    assert doctor_cmd._model_cached("multi-qa-mpnet-base-dot-v1") is False
    assert seen == ["multi-qa-mpnet-base-dot-v1", "sentence-transformers/multi-qa-mpnet-base-dot-v1"]
    assert doctor_cmd._model_cached("") is False


def _check_labels():
    import ast
    import inspect

    from commontrace.commands import doctor_cmd

    tree = ast.parse(inspect.getsource(doctor_cmd))
    labels = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_check", "_info") \
                and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant):
                labels.add(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                labels.add("".join(v.value if isinstance(v, ast.Constant) else "{}" for v in arg.values))
    return labels


def test_every_check_has_a_why_and_a_fix_so_the_guide_cannot_go_stale():
    from commontrace.commands import doctor_cmd

    labels = _check_labels()
    assert labels and not (labels - set(doctor_cmd.TROUBLESHOOTING)), \
        f"add a TROUBLESHOOTING entry for: {sorted(labels - set(doctor_cmd.TROUBLESHOOTING))}"
    unused = set(doctor_cmd.TROUBLESHOOTING) - labels
    assert not unused, f"TROUBLESHOOTING entries for checks that no longer exist: {sorted(unused)}"
    for label, (why, fix) in doctor_cmd.TROUBLESHOOTING.items():
        assert why.strip() and fix.strip(), label


def test_the_troubleshooting_guide_prints_and_exits_clean(capsys):
    from commontrace.cli import main

    assert main(["doctor", "--troubleshooting"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Troubleshooting") and "**Fix:**" in out and "## memory/ store present" in out


def test_a_failing_check_prints_its_fix(tmp_path, capsys, monkeypatch):
    from commontrace.cli import main

    monkeypatch.chdir(tmp_path)
    main(["doctor", "--dest", str(tmp_path / "nothing")])
    out = capsys.readouterr().out
    assert "[WARN] memory/ store present" in out and "fix: Run `commontrace init" in out


def test_a_world_readable_gateway_token_is_flagged(tmp_path, capsys, monkeypatch):
    import os

    from commontrace import gateway
    from commontrace.cli import main

    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    token = gateway.token_path(str(tmp_path))
    gateway.load_or_create_token(str(tmp_path))
    capsys.readouterr()
    main(["doctor", "--dest", str(tmp_path)])
    assert "[OK  ] gateway token file protected" in capsys.readouterr().out
    os.chmod(token, 0o644)
    main(["doctor", "--dest", str(tmp_path)])
    assert "[WARN] gateway token file protected" in capsys.readouterr().out


def test_a_run_that_never_reports_outcomes_and_a_compromised_one_are_named(tmp_path, capsys, monkeypatch):
    from commontrace import holdout_io
    from commontrace.cli import main

    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    config = holdout_io.configure(str(tmp_path), rate=0.5, salt="doc")
    for i in range(40):
        holdout_io.assign_and_log(str(tmp_path), ["m"], occasion_id=f"o{i}", rate=0.5 if i < 20 else 0.1,
                                  salt=config.salt, revisions={"m": "r"})
    capsys.readouterr()
    main(["doctor", "--dest", str(tmp_path)])
    out = capsys.readouterr().out
    assert "[WARN] experiment outcomes reported" in out and "fix: Report each occasion" in out
    assert "[WARN] experiment integrity" in out and "COMPROMISED" in out
