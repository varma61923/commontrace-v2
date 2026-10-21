from __future__ import annotations

import hashlib
import json

import pytest

from unimem import frontier_protocol as fp


@pytest.mark.parametrize("value", [10**1000, -(10**1000), float("nan"), float("inf"), True, None])
def test_numeric_boundaries_always_raise_validation_errors(value):
    with pytest.raises(ValueError):
        fp.number(value, -1e6, 1e6)


def test_json_bounds_are_checked_before_serialization(monkeypatch):
    def unexpected_serialization(*args, **kwargs):
        pytest.fail("oversized input reached JSON serialization")

    monkeypatch.setattr(fp.json, "dumps", unexpected_serialization)
    for value in ([None] * 200_001, {"payload": "x" * (fp.MAX_BYTES + 1)},
                  ["x" * 1024] * 1024, {"a" * 600_000: 0, "b" * 600_000: 1},
                  1 << (fp.MAX_BYTES * 4)):
        with pytest.raises(ValueError):
            fp.wire(value)


@pytest.mark.parametrize("raw", [
    b'{"record":{"kind":"narrow"},"record":{"kind":"wide"}}',
    b'{"record":{"kind":"safe","metadata":{"x":1,"x":2}}}',
    b'{"score":NaN}',
    b'{"score":Infinity}',
    b'{"score":-Infinity}',
])
def test_bounded_json_parser_rejects_ambiguous_and_nonfinite_values(raw):
    with pytest.raises(ValueError):
        fp.loads(raw)


@pytest.mark.parametrize("raw", [
    b'{"record":{"kind":"safe"},"record":{"kind":"safe"}}\n',
    b'{"record":{"score":NaN}}\n',
])
def test_signed_journal_reader_rejects_ambiguous_json_rows(tmp_path, raw):
    journal = tmp_path / "journal.jsonl"
    journal.write_bytes(raw)
    with pytest.raises(ValueError, match="journal contains invalid"):
        fp.rows(str(journal))


def test_canonical_encoding_and_hashes_remain_compatible():
    value = {"unicode": "日本語", "values": [0, 1.5, None, {"label": "internal"}]}
    previous = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    assert fp.wire(value) == previous
    assert fp.digest(value) == hashlib.sha256(previous).hexdigest()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_json_schema_numeric_types_reject_nonfinite_values_and_booleans(value):
    from unimem.validate import validate

    schema = {"properties": {"value": {"type": "number", "minimum": 0, "maximum": 1}}}
    assert validate({"value": value}, schema)
    assert not validate({"value": 0.5}, schema)


def test_untrusted_numeric_dates_and_optimizer_scores_fail_closed():
    from unimem import oauth, optimizer_intake, telemetry
    from unimem.validate import finite_number

    oversized = 10**1000
    assert not finite_number(oversized)
    assert not oauth._numeric_date(oversized)
    with pytest.raises(ValueError, match="finite"):
        optimizer_intake.candidate({"base_artifact": "base", "delta": "edit", "optimizer": "generic",
                                    "claimed_validation_score": oversized, "parent": None})
    with pytest.raises(ValueError, match="finite"):
        optimizer_intake.pareto([{"id": "x", "effect": oversized, "harm": 0, "supported": True}])
    assert not telemetry._valid_metric("events", oversized, {})
    assert not telemetry._valid_metric("events", 1, {3: "bad-key"})
    assert not telemetry._valid_metric("events" * 100, 1, {})
    assert finite_number(1.5) and finite_number(2) and not finite_number(True)


def test_oauth_backoff_saturates_during_prolonged_issuer_outages():
    from unimem import oauth

    config = oauth.OAuthConfig(issuer="https://issuer.example", audience="memory",
                              jwks_url="https://issuer.example/jwks")

    def unavailable(url):
        raise OSError("issuer offline")

    cache = oauth.JWKSCache(config, fetch=unavailable)
    cache._failures = 10_000
    with pytest.raises(oauth.KeysUnavailable) as error:
        cache.key_for("key", "RS256")
    assert error.value.retry_after == oauth.JWKS_RETRY_SECONDS[1]


def test_oauth_server_cache_is_bounded_and_keeps_recent_issuers(monkeypatch):
    from unimem import oauth

    monkeypatch.setattr(oauth, "_SERVERS", {})
    monkeypatch.setattr(oauth, "_MAX_SERVERS", 2)
    monkeypatch.setattr(oauth, "load_config", lambda root: oauth.OAuthConfig(
        issuer=f"https://{root}.example", audience="memory", jwks_url=f"https://{root}.example/jwks"))
    first = oauth.resource_server("first")
    second = oauth.resource_server("second")
    assert oauth.resource_server("first") is first
    oauth.resource_server("third")
    assert len(oauth._SERVERS) == 2
    assert oauth.resource_server("second") is not second
