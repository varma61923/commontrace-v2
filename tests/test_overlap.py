"""Tests for commontrace/overlap.py — the Fleet Overlap measurement.

Two things need guarding here, and they are different in kind:

1. **The estimator is correct.** A number that gets quoted to customers has
   to be checked against ground truth, not eyeballed.
2. **The disclosure boundary is what the docs say it is.** The first
   implementation claimed "no content is shared" and leaked "idempotency"
   via a lesson slug and "force-push" via a tag. These tests exist so that
   regression cannot recur silently.
"""
import json
import os

import pytest

from commontrace import overlap
from commontrace.cli import main


def _true_jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


class TestMinHashEstimator:
    def test_identical_text_is_similarity_one(self):
        sig = overlap.minhash("stripe webhook idempotency key retry")
        assert overlap.estimate_jaccard(sig, sig) == 1.0

    def test_disjoint_text_is_near_zero(self):
        a = overlap.minhash("alpha bravo charlie delta echo")
        b = overlap.minhash("xray yankee zulu whiskey victor")
        assert overlap.estimate_jaccard(a, b) < 0.1

    def test_estimate_tracks_true_jaccard_within_theoretical_error(self):
        """Standard error of MinHash is ~1/sqrt(num_perm). Assert the mean
        absolute error stays inside that bound over many random pairs --
        this is what makes the reported percentage trustworthy."""
        import random

        rng = random.Random(11)
        universe = [f"tok{i}" for i in range(200)]
        errors = []
        for _ in range(40):
            a = set(rng.sample(universe, 60))
            b = set(rng.sample(universe, 60))
            est = overlap.estimate_jaccard(
                overlap.minhash(" ".join(a)), overlap.minhash(" ".join(b))
            )
            errors.append(abs(est - _true_jaccard(a, b)))
        theoretical = 1 / (overlap.DEFAULT_NUM_PERM ** 0.5)
        assert sum(errors) / len(errors) < theoretical

    def test_signatures_are_stable_across_processes(self):
        """Python's hash() is randomized per process. If signing used it,
        two fleets would compute different signatures for identical text and
        every overlap would read as zero."""
        import subprocess
        import sys

        code = (
            "from commontrace import overlap; "
            "print(overlap.minhash('stripe webhook idempotency')[:4])"
        )
        env = dict(os.environ, PYTHONHASHSEED="0")
        a = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env).stdout
        env["PYTHONHASHSEED"] = "12345"
        b = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env).stdout
        assert a == b and a.strip()

    def test_empty_text_matches_nothing(self):
        """An empty activation condition must not match everything."""
        empty = overlap.minhash("")
        real = overlap.minhash("postgres connection pool exhausted on deploy")
        assert overlap.estimate_jaccard(empty, real) == 0.0

    def test_stopwords_do_not_create_false_overlap(self):
        a = overlap.minhash("the of and to in a for with")
        b = overlap.minhash("cuda kernel nondeterministic seed")
        assert overlap.estimate_jaccard(a, b) < 0.1

    def test_mismatched_signature_lengths_are_rejected(self):
        with pytest.raises(ValueError):
            overlap.estimate_jaccard(overlap.minhash("x", 64), overlap.minhash("x", 128))


def _item(label, kind, text, domain="", tags=None):
    return overlap.SignedItem(
        label=label, kind=kind, domain=domain, tags=tags or [],
        signature=overlap.minhash(text),
    )


class TestReport:
    def test_counts_only_failures_a_lesson_actually_covers(self):
        provider = overlap.FleetSignature("a", overlap.DEFAULT_NUM_PERM, [
            _item("lesson_pool", "lesson",
                  "rolling deploy exhausts postgres connections pool size replicas max_connections"),
        ])
        consumer = overlap.FleetSignature("b", overlap.DEFAULT_NUM_PERM, [
            _item("f1", "failure", "rolling deploy exhausted postgres connections pool size replicas max_connections"),
            _item("f2", "failure", "our bespoke enterprise discount margin formula miscalculated internally"),
        ])
        r = overlap.build_report(consumer, provider)
        assert r.n_failures == 2
        assert r.n_covered == 1
        assert r.matches[0].failure_label == "f1"

    def test_report_is_asymmetric(self):
        """'What would B gain from A' is not 'how alike are A and B'. A big
        mature fleet can cover a small one while gaining nothing back."""
        big = overlap.FleetSignature("big", overlap.DEFAULT_NUM_PERM, [
            _item("l1", "lesson", "postgres connection pool exhausted rolling deploy"),
        ])
        small = overlap.FleetSignature("small", overlap.DEFAULT_NUM_PERM, [
            _item("f1", "failure", "postgres connection pool exhausted rolling deploy"),
        ])
        assert overlap.build_report(small, big).n_covered == 1
        # big has no failures recorded, so it gains nothing measurable
        assert overlap.build_report(big, small).n_failures == 0

    def test_no_failures_produces_actionable_note_not_a_crash(self):
        r = overlap.build_report(
            overlap.FleetSignature("b", overlap.DEFAULT_NUM_PERM, []),
            overlap.FleetSignature("a", overlap.DEFAULT_NUM_PERM, [_item("l", "lesson", "x y z")]),
        )
        assert r.covered_fraction == 0.0
        assert "repeated_error" in r.note

    def test_mismatched_num_perm_is_rejected_with_a_clear_error(self):
        with pytest.raises(ValueError, match="num_perm"):
            overlap.build_report(
                overlap.FleetSignature("b", 64, []),
                overlap.FleetSignature("a", 128, []),
            )

    def test_small_sample_is_flagged_rather_than_quoted_confidently(self):
        provider = overlap.FleetSignature("a", overlap.DEFAULT_NUM_PERM, [_item("l", "lesson", "alpha beta gamma")])
        consumer = overlap.FleetSignature("b", overlap.DEFAULT_NUM_PERM, [_item("f", "failure", "alpha beta gamma")])
        assert "directional" in overlap.build_report(consumer, provider).note

    def test_higher_threshold_is_stricter(self):
        provider = overlap.FleetSignature("a", overlap.DEFAULT_NUM_PERM, [
            _item("l", "lesson", "postgres pool exhausted deploy replicas"),
        ])
        consumer = overlap.FleetSignature("b", overlap.DEFAULT_NUM_PERM, [
            _item("f", "failure", "postgres pool exhausted deploy something entirely different here"),
        ])
        assert overlap.build_report(consumer, provider, threshold=0.05).n_covered >= \
               overlap.build_report(consumer, provider, threshold=0.95).n_covered

    def test_render_does_not_crash_on_an_empty_report(self):
        out = overlap.render_report(
            overlap.build_report(
                overlap.FleetSignature("b", overlap.DEFAULT_NUM_PERM, []),
                overlap.FleetSignature("a", overlap.DEFAULT_NUM_PERM, []),
            )
        )
        assert "Fleet Overlap Report" in out


class TestDisclosureBoundary:
    """Regression guards for the leak found by testing the first version."""

    def test_signature_does_not_contain_source_text(self):
        secret = "proprietaryquantumpricingheuristic"
        sig = overlap.minhash(f"our {secret} misfired on tier three")
        assert secret not in json.dumps(sig)

    def test_redacted_label_hides_a_self_describing_slug(self):
        """`lesson_stripe_idempotency` describes its own content -- that was
        the actual leak. Redaction must remove it."""
        red = overlap.redact_label("lesson_stripe_idempotency")
        assert "stripe" not in red and "idempotency" not in red

    def test_redaction_is_stable_so_a_fleet_can_map_ids_back(self):
        assert overlap.redact_label("lesson_x") == overlap.redact_label("lesson_x")
        assert overlap.redact_label("lesson_x") != overlap.redact_label("lesson_y")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestOverlapCLI:
    def _seed(self, store):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        main([
            "lesson", "new", "--slug", "lesson_stripe_idempotency",
            "--description", "use idempotency keys on stripe webhooks",
            "--agent-type", "code", "--domain", "other", "--tags", "stripe,webhooks",
            "--applies-when", "a stripe webhook retries and creates duplicate charges",
            "--do-not-apply-when", "n/a", "--dest", str(store),
        ])
        main([
            "capture", "--title", "dup charges", "--context",
            "a stripe webhook retried and created duplicate charges",
            "--solution", "add key", "--tags", "stripe", "--agent-type", "code",
            "--repeated-error", "--dest", str(store),
        ])

    def test_sign_then_report_round_trip(self, store, tmp_path, capsys):
        self._seed(store)
        out = tmp_path / "sig.json"
        capsys.readouterr()
        assert main(["overlap", "sign", "--fleet-label", "f", "--out", str(out), "--dest", str(store)]) == 0
        assert out.is_file()
        # A fleet compared against itself covers its own failures.
        assert main(["overlap", "report", "--ours", str(out), "--theirs", str(out)]) == 0
        assert "Fleet Overlap Report" in capsys.readouterr().out

    def test_unredacted_sign_warns_that_slugs_and_tags_travel(self, store, tmp_path, capsys):
        self._seed(store)
        capsys.readouterr()
        main(["overlap", "sign", "--fleet-label", "f", "--out", str(tmp_path / "s.json"), "--dest", str(store)])
        captured = capsys.readouterr()
        assert "REAL slugs" in captured.out
        assert "Review it before sending" in captured.err

    def test_redacted_sign_leaks_no_source_terms(self, store, tmp_path, capsys):
        """End-to-end version of the leak regression: with both redaction
        flags, no distinctive source term appears anywhere in the file."""
        self._seed(store)
        out = tmp_path / "sig.json"
        main([
            "overlap", "sign", "--fleet-label", "f", "--out", str(out),
            "--dest", str(store), "--redact-labels", "--redact-tags",
        ])
        blob = out.read_text(encoding="utf-8").lower()
        for term in ("stripe", "idempotency", "webhook", "charges"):
            assert term not in blob, f"{term!r} leaked into the signature file"

    def test_redaction_does_not_change_the_measurement(self, store, tmp_path):
        """Redaction must cost nothing analytically -- it only touches
        labels/tags, never the signatures the estimate is computed from."""
        self._seed(store)
        plain, red = tmp_path / "p.json", tmp_path / "r.json"
        main(["overlap", "sign", "--fleet-label", "f", "--out", str(plain), "--dest", str(store)])
        main(["overlap", "sign", "--fleet-label", "f", "--out", str(red),
              "--dest", str(store), "--redact-labels", "--redact-tags"])
        p = overlap.FleetSignature.from_dict(json.loads(plain.read_text()))
        r = overlap.FleetSignature.from_dict(json.loads(red.read_text()))
        assert overlap.build_report(p, p).n_covered == overlap.build_report(r, r).n_covered

    def test_missing_signature_file_fails_cleanly(self, store, capsys):
        capsys.readouterr()
        assert main(["overlap", "report", "--ours", "/no/such.json", "--theirs", "/no/such.json"]) == 1
        assert "no such signature file" in capsys.readouterr().err
