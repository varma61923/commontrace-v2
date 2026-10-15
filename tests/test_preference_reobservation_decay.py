"""Preference reinforcement keeps prior decay; these checks do not open stores."""
from datetime import datetime, timedelta, timezone

import pytest

from commontrace.memory_record import MemoryRecord, preference_candidates, preference_statistics

AT = "2026-10-12T00:00:00Z"


def test_one_reobservation_does_not_restore_long_idle_accumulated_weight():
    prior = {"weight": 10, "observations": 10, "half_life_days": 90,
             "observed_at": "2026-04-15T00:00:00Z"}
    result = preference_statistics(prior, at=AT)
    assert result == {"weight": 3.5, "observations": 11, "half_life_days": 90}
    assert prior["weight"] == 10


@pytest.mark.parametrize("observed_at", ["2026-07-14T00:00:00Z", "2026-07-13T19:00:00-05:00",
                                         "2026-07-14T00:00:00"])
def test_reinforcement_applies_the_existing_half_life_at_one_instant(observed_at):
    result = preference_statistics({"weight": 8, "observations": 4, "half_life_days": 90,
                                    "observed_at": observed_at}, at=AT)
    assert result["weight"] == 5
    assert result["observations"] == 5


def test_custom_prior_half_life_is_used_before_new_default_policy():
    result = preference_statistics({"weight": 8, "half_life_days": 45,
                                    "observed_at": "2026-07-14T00:00:00Z"}, at=AT)
    assert result["weight"] == 3
    assert result["half_life_days"] == 90


def test_future_observation_never_amplifies_weight_and_caps_are_preserved():
    assert preference_statistics({"weight": 10, "observations": 1000000,
                                   "observed_at": "2027-01-01T00:00:00Z"}, at=AT) == {
        "weight": 10, "observations": 1000000, "half_life_days": 90,
    }


def test_legacy_missing_time_and_new_preference_keep_existing_semantics():
    assert preference_statistics({"weight": 1}, at=AT)["weight"] == 2
    assert preference_statistics({}, at=AT)["weight"] == 1


@pytest.mark.parametrize("change", [
    {"observed_at": "malformed"}, {"observed_at": 123}, {"half_life_days": 0},
    {"half_life_days": float("nan")}, {"half_life_days": True}, {"half_life_days": 36501},
])
def test_invalid_decay_metadata_cannot_revive_prior_accumulation(change):
    prior = {"weight": 10, "observations": 3, "observed_at": "2026-07-14T00:00:00Z", **change}
    result = preference_statistics(prior, at=AT)
    assert result["weight"] == 1
    assert result["observations"] == 4


def test_local_capture_uses_shared_decay_before_replacing_observation_time():
    from commontrace.framework_middleware import maintain_preferences

    scope = {"user": "alice", "agent": "writer"}
    identity, content = preference_candidates("I prefer concise answers.", scope)[0]
    old = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
    record = MemoryRecord(identity, "profile", 1,
                          {**content, "weight": 10, "observations": 10,
                           "half_life_days": 90, "observed_at": old}, scope=scope)

    class InMemoryJournal:
        latest_record = record

        def latest(self, candidate):
            assert candidate == identity
            return self.latest_record

        def put(self, updated):
            self.latest_record = updated

    journal = InMemoryJournal()
    maintain_preferences(journal, "I prefer concise answers.", scope)
    updated = journal.latest_record
    assert updated.version == 2
    assert updated.payload["weight"] == pytest.approx(3.5, abs=0.00001)
    assert updated.payload["observations"] == 11
    assert updated.payload["observed_at"] != old
    assert updated.scope == scope
