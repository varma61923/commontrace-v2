from __future__ import annotations

import pytest

from hub import bench_concurrency


class TestPercentile:
    def test_p50_of_a_known_list_is_a_real_sample(self):
        values = [0.001, 0.002, 0.003, 0.004]
        assert bench_concurrency._percentile(values, 50) in values

    def test_p100_is_the_slowest_request(self):
        assert bench_concurrency._percentile([0.5, 0.1, 0.9, 0.3], 100) == 0.9

    def test_the_result_is_always_an_observed_value_never_an_interpolation(self):
        values = [0.010, 0.020, 0.080]
        for pct in (1, 25, 50, 75, 95, 99, 100):
            assert bench_concurrency._percentile(values, pct) in values

    def test_unsorted_input_is_handled(self):
        assert bench_concurrency._percentile([0.3, 0.1, 0.2], 50) == 0.2

    def test_p99_of_a_hundred_samples_is_the_second_slowest_or_worse(self):
        values = [float(i) / 1000.0 for i in range(1, 101)]
        assert bench_concurrency._percentile(values, 99) == 0.099

    def test_an_empty_list_is_zero_rather_than_an_exception(self):
        assert bench_concurrency._percentile([], 95) == 0.0

    def test_a_single_sample_is_that_sample_at_every_percentile(self):
        assert bench_concurrency._percentile([0.42], 50) == 0.42
        assert bench_concurrency._percentile([0.42], 99) == 0.42


class TestVerdict:
    def test_no_speedup_at_all_reads_as_serialized(self):
        assert bench_concurrency.verdict(1.0) == "SERIALIZED"

    def test_linear_speedup_reads_as_scaling(self):
        assert bench_concurrency.verdict(8.0) == "scales"

    def test_the_middle_band_is_reported_as_partial(self):
        assert bench_concurrency.verdict(2.5) == "partial"

    def test_an_unmeasured_level_is_not_silently_a_verdict(self):
        assert bench_concurrency.verdict(None) == "not measured"

    def test_the_bands_do_not_overlap_or_leave_a_gap(self):
        seen = {
            bench_concurrency.verdict(x / 10.0)
            for x in range(1, 200)
        }
        assert seen == {"SERIALIZED", "partial", "scales"}

    def test_the_boundary_is_inclusive_on_the_serialized_side(self):
        assert bench_concurrency.verdict(bench_concurrency._SERIALIZED_SPEEDUP) == "SERIALIZED"
        assert bench_concurrency.verdict(bench_concurrency._SCALES_SPEEDUP) == "scales"


class TestArgumentParsing:
    def test_non_integer_client_counts_are_refused(self, capsys):
        assert bench_concurrency.main(["--clients", "8,abc"]) == 2
        assert "comma-separated integers" in capsys.readouterr().err

    def test_an_empty_client_list_is_refused(self, capsys):
        assert bench_concurrency.main(["--clients", ","]) == 2
        assert "at least one" in capsys.readouterr().err

    def test_an_unknown_backend_is_refused(self):
        with pytest.raises(SystemExit):
            bench_concurrency.main(["--backend", "redis"])
