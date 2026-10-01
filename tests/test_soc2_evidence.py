"""The SOC 2 mapping stays true: every file, symbol and test count it cites exists (hub/soc2_evidence.py)."""
import pytest

soc2 = pytest.importorskip("hub.soc2_evidence")


def test_every_reference_in_the_readiness_document_resolves():
    report = soc2.build()
    assert report.refs, "the document cites nothing"
    assert not report.dangling, [(r.raw, r.problem) for r in report.dangling]


def test_parse_reads_paths_symbols_and_test_counts_and_ignores_what_is_not_a_file():
    text = ("| Criterion | Control | Evidence |\n|---|---|---|\n"
            "| A | c | `hub/auth.py:verify_api_key`, `hub/tests/test_auth.py` (3 tests), `HUB_ENCRYPTION_KEY`, "
            "`/healthz` |\n| B | c | `hub/tests/test_manage.py::TestX::test_y` |\n")
    refs = soc2.parse(text)
    assert [(r.path, r.symbol, r.claimed_tests) for r in refs] == [
        ("hub/auth.py", "verify_api_key", None), ("hub/tests/test_auth.py", "", 3),
        ("hub/tests/test_manage.py", "TestX::test_y", None)]


def test_a_missing_file_and_a_missing_symbol_are_each_dangling(tmp_path):
    (tmp_path / "hub").mkdir()
    (tmp_path / "hub" / "tests").mkdir()
    (tmp_path / "hub" / "a.py").write_text("def real(): pass\n")
    (tmp_path / "hub" / "tests" / "test_a.py").write_text("def test_one(): pass\n")
    cases = [("hub/missing.py", "", None, "does not exist"), ("hub/a.py", "gone", None, "not found")]
    for path, symbol, count, message in cases:
        ref = soc2.resolve(soc2.Ref(control="c", raw=path, path=path, symbol=symbol, claimed_tests=count),
                           str(tmp_path))
        assert not ref.ok and message in ref.problem
    assert soc2.resolve(soc2.Ref(control="c", raw="x", path="hub/a.py", symbol="real"), str(tmp_path)).ok


def test_the_bundle_says_what_it_is_not():
    text = soc2.render(soc2.build())
    assert "not a Type II report" in text and "Commit" in text


def test_a_claimed_test_count_is_checked_against_what_pytest_runs(tmp_path):
    (tmp_path / "hub" / "tests").mkdir(parents=True)
    (tmp_path / "hub" / "tests" / "test_a.py").write_text("def test_one(): pass\n")
    ref = soc2.Ref(control="c", raw="x", path="hub/tests/test_a.py", claimed_tests=5)
    report = soc2.Report(refs=[ref])
    soc2.run_tests(report, str(tmp_path))
    assert report.tests[0]["passed"] is False and "claims 5" in report.tests[0]["summary"]
    ref.claimed_tests = 1
    report = soc2.Report(refs=[ref])
    soc2.run_tests(report, str(tmp_path))
    assert report.tests[0]["passed"] is True
