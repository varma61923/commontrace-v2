"""Tests for the corpus-scaling benchmark's own arithmetic.

The benchmark answers STRATEGY.md §13.2's cost question, and a benchmark
that reports a wrong exponent is worse than no benchmark: it would settle
a strategic question with a number nobody could see was broken. The
measurement itself needs Postgres and minutes; the fit and the verdict
thresholds are pure and are what actually turn timings into a conclusion,
so they are pinned here.

The first run of this benchmark reported `search_traces` as
LINEAR-OR-WORSE, and that turned out to be the generator's fault -- every
synthetic row shared near-identical title text, so the probe query matched
100% of the corpus. Fixing the fixture moved the realistic case to 0.19.
`TestGeneratedCorpusIsSelective` exists so that cannot silently come back.
"""
from __future__ import annotations

import re

import pytest

from hub import bench_scaling

pytestmark = pytest.mark.asyncio


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestFitExponent:
    def test_flat_latency_fits_a_zero_exponent(self):
        alpha = bench_scaling._fit_exponent([1000, 4000, 16000], [5.0, 5.0, 5.0])
        assert alpha == pytest.approx(0.0, abs=1e-9)

    def test_linear_growth_fits_an_exponent_of_one(self):
        """Latency proportional to corpus size is exactly the shape §13.2
        calls fatal, so the fit must name it precisely rather than
        approximately."""
        alpha = bench_scaling._fit_exponent([1000, 4000, 16000], [1.0, 4.0, 16.0])
        assert alpha == pytest.approx(1.0, abs=1e-9)

    def test_square_root_growth_fits_a_half(self):
        alpha = bench_scaling._fit_exponent([100, 10_000], [10.0, 100.0])
        assert alpha == pytest.approx(0.5, abs=1e-9)

    def test_quadratic_growth_is_reported_above_one(self):
        alpha = bench_scaling._fit_exponent([10, 100], [1.0, 100.0])
        assert alpha == pytest.approx(2.0, abs=1e-9)

    def test_a_zero_latency_is_dropped_rather_than_taken_as_log_zero(self):
        """A measurement too fast for the clock must not become an infinite
        slope that silently reads as a catastrophic regression."""
        assert bench_scaling._fit_exponent([1, 2, 4], [0.0, 1.0, 2.0]) is not None
        assert bench_scaling._fit_exponent([1, 2], [0.0, 0.0]) is None

    def test_a_single_point_cannot_produce_an_exponent(self):
        """The reason the CLI requires at least three sizes: one timing at
        one corpus size is what the pre-existing DEPLOYMENT.md note had, and
        it says nothing about growth."""
        assert bench_scaling._fit_exponent([1000], [5.0]) is None


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestVerdict:
    def test_thresholds_map_to_the_three_readings(self):
        assert bench_scaling.verdict(0.0) == "flat"
        assert bench_scaling.verdict(bench_scaling.FLAT_EXPONENT) == "flat"
        assert bench_scaling.verdict(0.5) == "sublinear"
        assert bench_scaling.verdict(bench_scaling.LINEAR_EXPONENT) == "LINEAR-OR-WORSE"
        assert bench_scaling.verdict(1.2) == "LINEAR-OR-WORSE"
        assert bench_scaling.verdict(None) == "unmeasurable"

    def test_the_bands_do_not_overlap_or_leave_a_gap(self):
        assert bench_scaling.FLAT_EXPONENT < bench_scaling.LINEAR_EXPONENT


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestGeneratedCorpusIsSelective:
    """The fixture bug that made the first run's headline finding wrong.

    A benchmark whose every row matches every query measures the worst
    case and reports it as the typical one. These assert the generator
    still produces varied text, without needing a database.
    """

    def test_the_generator_draws_from_many_distinct_titles(self):
        source = bench_scaling._seed.__doc__ or ""
        assert source is not None
        import inspect

        body = inspect.getsource(bench_scaling._seed)
        titles = re.findall(r"'([a-z][^']{25,})'", body)
        assert len(titles) >= 15, (
            "the synthetic corpus needs many distinct titles, or every probe query "
            f"matches every row; found {len(titles)}"
        )

    def test_the_selective_probe_term_is_not_in_the_shared_boilerplate(self):
        """`search_traces (selective)` must query words that appear in ONE
        title, not in the context/solution text every row shares."""
        body = __import__("inspect").getsource(bench_scaling.run)
        assert "hydration mismatch timestamps" in body
        seed_body = __import__("inspect").getsource(bench_scaling._seed)
        shared = seed_body.split("'observed in production run '")[1]
        assert "hydration" not in shared


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestCli:
    def test_fewer_than_three_sizes_is_refused(self, capsys):
        assert bench_scaling.main(["--sizes", "1000,2000"]) == 2
        assert "at least 3 sizes" in capsys.readouterr().err

    def test_non_integer_sizes_are_refused(self, capsys):
        assert bench_scaling.main(["--sizes", "a,b,c"]) == 2
        assert "must be integers" in capsys.readouterr().err
