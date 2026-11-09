"""Experiment settings remain intact while readers and failed writers overlap."""

import pytest

from unimem import holdout_io


def test_readers_keep_the_committed_configuration_during_publication(tmp_path, monkeypatch):
    root = str(tmp_path)
    before = holdout_io.configure(root, rate=0.2, salt="committed")
    dump = holdout_io.json.dump
    observed = []

    def inspect(value, stream, **kwargs):
        observed.append(holdout_io.load_config(root))
        return dump(value, stream, **kwargs)

    monkeypatch.setattr(holdout_io.json, "dump", inspect)
    after = holdout_io.configure(root, rate=0.7, salt="replacement")
    assert observed == [before]
    assert holdout_io.load_config(root) == after


def test_failed_publication_preserves_the_previous_experiment(tmp_path, monkeypatch):
    root = str(tmp_path)
    before = holdout_io.configure(root, rate=0.2, salt="committed")

    def interrupted(value, stream, **kwargs):
        stream.write('{"rate":')
        raise OSError("disk full")

    monkeypatch.setattr(holdout_io.json, "dump", interrupted)
    with pytest.raises(OSError, match="disk full"):
        holdout_io.configure(root, rate=0.7, salt="replacement")
    assert holdout_io.load_config(root) == before
    assert not list((tmp_path / "memory").glob(".experiment.json.*.tmp"))


@pytest.mark.parametrize(
    "options",
    [
        {"rate": False},
        {"rate": "0.5"},
        {"rate": None},
        {"rate": float("nan")},
        {"rate": float("inf")},
        {"rate": 10**400},
        {"detect": "0.1"},
        {"detect": None},
        {"salt": 42},
        {"note": {"invalid": "object"}},
    ],
)
def test_invalid_configuration_never_replaces_committed_settings(tmp_path, options):
    root = str(tmp_path)
    before = holdout_io.configure(root, rate=0.2, salt="committed")
    with pytest.raises(ValueError):
        holdout_io.configure(root, **{"rate": 0.5, **options})
    assert holdout_io.load_config(root) == before
