"""Optional record-envelope vectors; the causal v1 vectors are untouched."""
from __future__ import annotations

import json
import math
import os

import pytest

from unimem.memory_record import MemoryRecord

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_ROOT, "protocol", "conformance", "memory-record-vectors.json"), encoding="utf-8") as source:
    _VECTORS = json.load(source)


@pytest.mark.parametrize("record", _VECTORS["valid"])
def test_valid_vectors(record):
    envelope = MemoryRecord(**record).to_dict()
    jsonschema = pytest.importorskip("jsonschema")
    with open(os.path.join(_ROOT, "protocol", "schemas", "memory-record.schema.json"), encoding="utf-8") as source:
        jsonschema.Draft202012Validator(json.load(source)).validate(envelope)


@pytest.mark.parametrize("record", _VECTORS["invalid"])
def test_invalid_vectors(record):
    with pytest.raises(ValueError):
        MemoryRecord(**record)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_memory_envelopes_reject_non_finite_nested_numbers(value):
    with pytest.raises(ValueError, match="finite"):
        MemoryRecord("non-finite", "fact", 1, {"text": "safe", "score": value})


def test_memory_envelope_rejects_oversized_integers_before_numeric_validation():
    with pytest.raises(ValueError, match="integer exceeds"):
        MemoryRecord("oversized-integer", "fact", 1, {"score": 1 << 4096})


def test_memory_envelopes_reject_cycles_and_non_string_object_keys():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="cycles"):
        MemoryRecord("cycle", "fact", 1, {"nested": cycle})
    with pytest.raises(ValueError, match="keys must be strings"):
        MemoryRecord("key", "fact", 1, {"nested": {1: "invalid"}})


def test_memory_envelope_revalidates_mutable_payload_before_serialization():
    record = MemoryRecord("mutable", "fact", 1, {"text": "safe"})
    record.payload["score"] = math.nan
    with pytest.raises(ValueError, match="finite"):
        record.to_dict()


def test_memory_envelope_has_a_bounded_json_wire_size():
    from unimem.memory_record import MAX_RECORD_BYTES

    with pytest.raises(ValueError, match="one mebibyte"):
        MemoryRecord("large", "fact", 1, {"text": "x" * MAX_RECORD_BYTES})


@pytest.mark.parametrize("changes", [
    {field: value}
    for field in ("created_at", "valid_from", "valid_until", "retracted_at")
    for value in (False, 0, [], {})
] + [{"created_at": None}, {"evidence": [None]}, {"evidence": ["citation"]}])
def test_untrusted_record_put_rejects_invalid_envelopes_before_persistence(tmp_path, changes):
    from unimem.memory_journal import Journal
    from unimem.record_api import operate

    record = {"id": "invalid", "kind": "fact", "version": 1, "payload": {"text": "fact"}, **changes}
    with pytest.raises(ValueError):
        operate(str(tmp_path), "record-put", {"record": record}, context=["agent:review"], untrusted=True)
    assert Journal(str(tmp_path)).latest("invalid") is None
