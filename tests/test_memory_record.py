"""Optional record-envelope vectors; the causal v1 vectors are untouched."""
from __future__ import annotations

import json
import os

import pytest

from commontrace.memory_record import MemoryRecord

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
