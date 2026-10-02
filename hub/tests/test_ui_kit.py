from __future__ import annotations

import re
from datetime import datetime, timezone

from hub import ui_kit

WEEKS = ["2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28"]


class TestCountAxis:
    def test_ticks_are_whole_numbers(self):
        for peak in (0, 1, 2, 3, 7, 13, 99, 250, 1234):
            top, step = ui_kit._count_axis(peak)
            assert step >= 1 and step == int(step), (peak, step)
            assert top >= max(1, peak)
            assert top / step == int(top / step)

    def test_about_four_ticks(self):
        for peak in (8, 40, 400, 4000):
            top, step = ui_kit._count_axis(peak)
            assert 2 <= top / step <= 8


class TestBarChart:
    def test_one_bar_per_week_with_table_view_and_tooltips(self):
        out = ui_kit.bar_chart("t", WEEKS, [0, 3, 1, 2], title="Traces", unit="trace")
        assert out.count('class="viz-bar-g"') == len(WEEKS)
        assert "Show the data as a table" in out
        assert out.count("<tr>") == len(WEEKS) + 1
        assert 'role="img"' in out and "<title" in out

    def test_title_is_escaped(self):
        out = ui_kit.bar_chart("t", WEEKS, [1, 1, 1, 1], title="<script>x</script>", unit="trace")
        assert "<script>x</script>" not in out
        assert "&lt;script&gt;" in out


class TestRateChart:
    def test_a_thin_week_is_a_gap_not_a_point(self):
        treated = [[20, 10], [2, 2], [20, 15], [20, 16]]
        control = [[0, 0], [0, 0], [0, 0], [0, 0]]
        out = ui_kit.rate_chart("r", WEEKS, treated, control, title="Rates")
        path = re.search(r'<path class="viz-line viz-l1" d="([^"]+)"', out)
        assert path is not None
        moves = path.group(1).count("M")
        assert moves == 2, f"expected the line to break at the thin week, got {path.group(1)!r}"
        assert "With 100%" not in out

    def test_both_series_are_labelled_and_legended(self):
        treated = [[10, 8]] * 4
        control = [[10, 5]] * 4
        out = ui_kit.rate_chart("r", WEEKS, treated, control, title="Rates")
        assert "With 80%" in out and "Without 50%" in out
        assert "With memory" in out and "Without memory" in out
        assert "Show the data as a table" in out


class TestForestPlot:
    def test_rows_are_a_valid_table_structure(self):
        out = ui_kit.forest_plot([
            {"trace_id": "a", "title": "Alpha", "effect": 0.2, "ci_95": [0.05, 0.35],
             "verdict": "HELPS", "significant": True},
            {"trace_id": "b", "title": "<b>Beta</b>", "effect": -0.1, "ci_95": [-0.3, 0.1],
             "verdict": "UNDERPOWERED", "significant": False},
        ])
        assert out.count('role="row"') == 3
        assert out.count('role="cell"') == 6
        assert "<b>Beta</b>" not in out and "&lt;b&gt;Beta&lt;/b&gt;" in out
        assert "f-ok" in out and "f-mute" in out

    def test_nothing_to_plot_renders_nothing(self):
        assert ui_kit.forest_plot([]) == ""
        assert ui_kit.forest_plot([{"trace_id": "a", "effect": None}]) == ""


class TestSmallHelpers:
    def test_time_html_is_machine_readable_and_utc(self):
        out = ui_kit.time_html(datetime(2026, 9, 29, 1, 32, tzinfo=timezone.utc))
        assert 'datetime="2026-09-29T01:32:00+00:00"' in out
        assert "Sep 29, 2026 01:32 UTC" in out and "data-rel" in out

    def test_time_html_never_echoes_markup(self):
        out = ui_kit.time_html("<img src=x onerror=alert(1)>")
        assert "<img" not in out

    def test_meter_tones_only_for_quotas(self):
        assert "meter bad" in ui_kit.meter(10, 10)
        assert "meter warn" in ui_kit.meter(8, 10)
        assert 'class="meter"' in ui_kit.meter(10, 10, quota=False)
        assert ui_kit.meter(5, 0) == ""
        assert ui_kit.meter("x", 10) == ""

    def test_initials(self):
        assert ui_kit.initials("Northwind Robotics") == "NR"
        assert ui_kit.initials("") != ""
