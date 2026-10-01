"""commontrace/outcome_detect.py -- each detector's defined boundary, and
the ambiguous cases that must return None rather than a guess. This module
makes no precision claim against a labelled sample (that needs a real
fleet's data, see the module's own docstring); what is tested here is that
the mapping each function documents is the mapping it implements.
"""
from __future__ import annotations

import pytest

from commontrace import outcome_detect as od


class TestFromTestExitCode:
    def test_zero_is_success(self):
        assert od.from_test_exit_code(0) is True

    def test_one_is_failure(self):
        assert od.from_test_exit_code(1) is False

    @pytest.mark.parametrize("code", [2, 3, 4, 5, 130])
    def test_other_codes_are_ambiguous(self, code):
        assert od.from_test_exit_code(code) is None


class TestFromRetryCount:
    def test_within_the_acceptable_count_is_success(self):
        assert od.from_retry_count(2, max_acceptable=3) is True
        assert od.from_retry_count(3, max_acceptable=3) is True

    def test_over_the_acceptable_count_is_failure(self):
        assert od.from_retry_count(4, max_acceptable=3) is False

    def test_zero_retries_is_success(self):
        assert od.from_retry_count(0, max_acceptable=0) is True

    def test_negative_retries_is_rejected(self):
        with pytest.raises(ValueError, match="retries"):
            od.from_retry_count(-1, max_acceptable=3)

    def test_negative_max_acceptable_is_rejected(self):
        with pytest.raises(ValueError, match="max_acceptable"):
            od.from_retry_count(0, max_acceptable=-1)


class TestFromTicketTransition:
    RESOLVED = frozenset({"resolved", "closed"})
    REOPENED = frozenset({"reopened"})

    def test_a_resolved_status_is_success(self):
        assert od.from_ticket_transition(
            "closed", resolved_statuses=self.RESOLVED, reopened_statuses=self.REOPENED,
        ) is True

    def test_a_reopened_status_is_failure(self):
        assert od.from_ticket_transition(
            "reopened", resolved_statuses=self.RESOLVED, reopened_statuses=self.REOPENED,
        ) is False

    def test_an_unlisted_status_is_ambiguous(self):
        assert od.from_ticket_transition(
            "pending", resolved_statuses=self.RESOLVED, reopened_statuses=self.REOPENED,
        ) is None

    def test_vendor_agnostic_vocabularies_work_identically(self):
        jira = {"resolved_statuses": frozenset({"Done"}), "reopened_statuses": frozenset({"Reopened"})}
        assert od.from_ticket_transition("Done", **jira) is True
        assert od.from_ticket_transition("Reopened", **jira) is False


class TestFromCsat:
    def test_above_the_default_threshold_is_success(self):
        assert od.from_csat(4, scale_max=5) is True  # 0.8 >= 0.6

    def test_below_the_default_threshold_is_failure(self):
        assert od.from_csat(2, scale_max=5) is False  # 0.4 < 0.6

    def test_a_different_scale_is_normalized_the_same_way(self):
        assert od.from_csat(80, scale_max=100) is True   # 0.8 >= 0.6
        assert od.from_csat(40, scale_max=100) is False  # 0.4 < 0.6

    def test_a_custom_threshold_fraction_is_honoured(self):
        assert od.from_csat(3, scale_max=5, threshold_fraction=0.5) is True  # 0.6 >= 0.5

    def test_zero_scale_max_is_rejected(self):
        with pytest.raises(ValueError, match="scale_max"):
            od.from_csat(1, scale_max=0)

    @pytest.mark.parametrize("fraction", [0.0, 1.0, -0.1, 1.5])
    def test_an_out_of_range_threshold_fraction_is_rejected(self, fraction):
        with pytest.raises(ValueError, match="threshold_fraction"):
            od.from_csat(1, scale_max=5, threshold_fraction=fraction)


class TestFromHumanTakeover:
    def test_a_takeover_is_read_as_not_resolved_autonomously(self):
        assert od.from_human_takeover(True) is False

    def test_no_takeover_is_read_as_resolved(self):
        assert od.from_human_takeover(False) is True
