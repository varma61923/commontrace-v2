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
        assert od.from_csat(4, scale_max=5) is True

    def test_below_the_default_threshold_is_failure(self):
        assert od.from_csat(2, scale_max=5) is False

    def test_a_different_scale_is_normalized_the_same_way(self):
        assert od.from_csat(80, scale_max=100) is True
        assert od.from_csat(40, scale_max=100) is False

    def test_a_custom_threshold_fraction_is_honoured(self):
        assert od.from_csat(3, scale_max=5, threshold_fraction=0.5) is True

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


from datetime import datetime, timedelta, timezone  # noqa: E402

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _days(n):
    return T0 + timedelta(days=n)


def test_event_within_window():
    kw = dict(started_at=T0, window_days=14)
    assert od.from_event_within_window(_days(3), **kw) is True
    assert od.from_event_within_window(_days(14), **kw) is True
    assert od.from_event_within_window(_days(15), **kw) is False
    assert od.from_event_within_window(None, now=_days(5), **kw) is None
    assert od.from_event_within_window(None, now=_days(20), **kw) is False
    assert od.from_event_within_window(_days(-1), **kw) is None


def test_no_reversal_is_only_known_at_the_end_of_the_window():
    kw = dict(started_at=T0, window_days=7)
    assert od.from_no_reversal(None, now=_days(3), **kw) is None
    assert od.from_no_reversal(None, now=_days(8), **kw) is True
    assert od.from_no_reversal(_days(2), now=_days(3), **kw) is False
    assert od.from_no_reversal(_days(30), now=_days(31), **kw) is True
    assert od.from_no_reversal(_days(-1), **kw) is None


def test_naive_and_aware_datetimes_compare():
    assert od.from_event_within_window(datetime(2026, 9, 2), started_at=T0, window_days=7) is True


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_a_window_must_be_a_positive_finite_number(bad):
    with pytest.raises(ValueError):
        od.from_event_within_window(None, started_at=T0, window_days=bad)
    with pytest.raises(ValueError):
        od.from_no_reversal(None, started_at=T0, window_days=bad)


def test_threshold():
    assert od.from_threshold(0.9, minimum=0.8) is True
    assert od.from_threshold(0.7, minimum=0.8) is False
    assert od.from_threshold(3.0, maximum=5.0) is True
    assert od.from_threshold(6.0, minimum=1.0, maximum=5.0) is False
    assert od.from_threshold(None, minimum=0.8) is None
    assert od.from_threshold(float("nan"), minimum=0.8) is None
    with pytest.raises(ValueError):
        od.from_threshold(1.0)
    with pytest.raises(ValueError):
        od.from_threshold(1.0, minimum=5, maximum=1)


def test_from_all_three_valued_logic():
    assert od.from_all(True, True) is True
    assert od.from_all(True, False, None) is False
    assert od.from_all(True, None) is None
    with pytest.raises(ValueError):
        od.from_all()


def test_from_any_three_valued_logic():
    assert od.from_any(False, True, None) is True
    assert od.from_any(False, None) is None
    assert od.from_any(False, False) is False
    with pytest.raises(ValueError):
        od.from_any()
