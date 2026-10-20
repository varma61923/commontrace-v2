"""UniMem optimizer proposals are hypotheses, not evidence of improvement.

GEPA (arXiv:2507.19457), ACE (arXiv:2510.04618), Training-Free GRPO
(arXiv:2510.08191). Adapters below accept documented UniMem export envelopes;
GEPA/DSPy serialization subsets are checked against pinned source and authored
fixtures; other vendor formats remain unverified. Canonical randomized rows, never claimed validation scores, decide
promotion and Pareto membership. This module executes no proposed code.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from commontrace.validate import finite_number

FORMATS = ("generic", "gepa", "ace", "training-free-grpo", "dspy", "code-diff")
GEPA_SCHEMA_COMMIT = "f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819"
DSPY_SCHEMA_COMMIT = "c7e9752debcce5715fee62cd5f1ae538c14d5ce4"


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= maximum or '\x00' in value:
        raise ValueError("UniMem invalid optimizer " + label)
    return value


def _digest(value: Any) -> str:
    pending = [(value, 0)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if depth > 16 or visited > 10000:
            raise ValueError("UniMem optimizer JSON exceeds its depth or node bound")
        if isinstance(item, (dict, list, tuple)) and visited + len(pending) + len(item) > 10000:
            raise ValueError("UniMem optimizer JSON exceeds its node bound")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("UniMem optimizer JSON keys must be strings")
            pending.extend((child, depth+1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth+1) for child in item)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    if len(raw) > 128 * 1024:
        raise ValueError("UniMem optimizer candidate exceeds 128 KiB")
    return hashlib.sha256(raw).hexdigest()


def candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {"base_artifact", "delta", "optimizer", "claimed_validation_score", "parent"}
    if set(value) != required:
        raise ValueError("UniMem optimizer candidate fields do not match the generic schema")
    base = _text(value["base_artifact"], "base artifact", 256)
    delta = _text(value["delta"], "delta", 60000)
    optimizer = _text(value["optimizer"], "name", 128)
    parent = value["parent"]
    if parent is not None:
        _text(parent, "parent", 256)
    score = value["claimed_validation_score"]
    if score is not None and not finite_number(score):
        raise ValueError("UniMem claimed validation score must be finite or null")
    body = {"base_artifact": base, "delta": delta, "optimizer": optimizer, "parent": parent}
    identity = "delta:" + _digest(body)
    return {"id": identity, **body, "claimed_validation_score": score,
            "artifact_hash": _digest(body), "content_hash": _digest(delta),
            "classification": "unverified proposal"}


def adapt(format: str, value: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Bounded UniMem interchange adapters; no import of an optimizer SDK."""
    if format not in FORMATS:
        raise ValueError("UniMem unknown optimizer format")
    if not isinstance(value, Mapping):
        raise ValueError("UniMem optimizer intake must be an object")
    if format == "generic":
        return [{**candidate(value), "adapter": "generic", "format_status": "UniMem v1"}]
    _digest(dict(value))
    base = _text(value.get("base_artifact"), "base artifact", 256)
    parent = value.get("parent")
    if format == "gepa" and "gepa_result" in value:
        return _gepa(base, value["gepa_result"])
    if format == "dspy" and "dspy_state" in value:
        return _dspy(base, value["dspy_state"], parent)
    field = {"gepa": "candidates", "ace": "bullets", "training-free-grpo": "experiences",
             "dspy": "programs", "code-diff": "diffs"}[format]
    edits = value.get(field)
    if not isinstance(edits, list) or not 1 <= len(edits) <= 64:
        raise ValueError("UniMem optimizer export must contain 1..64 edits in " + field)
    result = []
    for edit in edits:
        delta: Any
        if isinstance(edit, str):
            delta, score = edit, None
        elif isinstance(edit, Mapping) and set(edit) <= {"delta", "score", "parent"}:
            delta, score = edit.get("delta"), edit.get("score")
        else:
            raise ValueError("UniMem optimizer export edits require delta, optional score and parent")
        proposed = candidate({"base_artifact": base, "delta": delta, "optimizer": format,
                              "claimed_validation_score": score,
                              "parent": edit.get("parent", parent) if isinstance(edit, Mapping) else parent})
        result.append({**proposed, "adapter": format, "format_status": "format unverified",
                       "unit_kind": "bullet" if format == "ace" else "candidate"})
    return result


def _json_text(value: Any) -> str:
    return _text(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False),
                 "serialized program", 60000)


def _gepa(base: str, state: Any) -> list[dict[str, Any]]:
    """Read the pinned GEPAResult.to_dict candidate/score/parent subset."""
    if not isinstance(state, dict):
        raise ValueError("UniMem GEPA result must be a JSON object")
    programs, parents, scores = (state.get(key) for key in ("candidates", "parents", "val_aggregate_scores"))
    if (not isinstance(programs, list) or not 1 <= len(programs) <= 64
            or not isinstance(parents, list) or not isinstance(scores, list)
            or len(parents) != len(programs) or len(scores) != len(programs)):
        raise ValueError("UniMem GEPA candidates, parents and scores must have matching bounded lengths")
    result = []
    for index, program in enumerate(programs):
        if not isinstance(program, dict) or not 1 <= len(program) <= 16:
            raise ValueError("UniMem GEPA candidates require 1..16 named prompt strings")
        for key, text in program.items():
            _text(key, "GEPA component", 128)
            _text(text, "GEPA prompt", 48000)
        ancestors = parents[index]
        if not isinstance(ancestors, list) or len(ancestors) > 16:
            raise ValueError("UniMem GEPA parent rows exceed their bound")
        indices = []
        for ancestor in ancestors:
            if ancestor is None:
                continue
            if type(ancestor) is not int or not 0 <= ancestor < index or ancestor in indices:
                raise ValueError("UniMem GEPA parent indices must be unique earlier candidates")
            indices.append(ancestor)
        parent = _digest([programs[i] for i in sorted(indices)]) if indices else None
        value = candidate({"base_artifact": base, "delta": _json_text(program), "parent": parent,
                           "optimizer": "gepa", "claimed_validation_score": scores[index]})
        result.append({**value, "adapter": "gepa", "format_status": "verified serialization subset",
                       "schema_commit": GEPA_SCHEMA_COMMIT, "delta_encoding": "replacement-prompt-map-json",
                       "unit_kind": "candidate"})
    return result


def _dspy(base: str, state: Any, parent: Any) -> list[dict[str, Any]]:
    """Extract prompt state, never deserialize LM objects or retain credentials."""
    if not isinstance(state, dict) or not 1 <= len(state) <= 17:
        raise ValueError("UniMem DSPy state requires at most 16 predictors and optional metadata")
    predictors = {}
    for name, predictor in state.items():
        if name == "metadata":
            continue
        _text(name, "DSPy predictor name", 128)
        if not isinstance(predictor, dict) or not isinstance(predictor.get("signature"), dict):
            raise ValueError("UniMem DSPy predictor requires its serialized signature")
        signature = predictor["signature"]
        instructions = _text(signature.get("instructions"), "DSPy instructions", 48000)
        fields = signature.get("fields")
        if not isinstance(fields, list) or len(fields) > 64:
            raise ValueError("UniMem DSPy signature fields exceed their bound")
        clean_fields = []
        for field in fields:
            if not isinstance(field, dict) or set(field) != {"prefix", "description"}:
                raise ValueError("UniMem DSPy signature fields require prefix and description")
            if any(not isinstance(v, str) or len(v) > 4096 for v in field.values()):
                raise ValueError("UniMem DSPy signature field text exceeds its bound")
            clean_fields.append(dict(field))
        demos = predictor.get("demos", [])
        if not isinstance(demos, list) or len(demos) > 64 or any(not isinstance(d, dict) for d in demos):
            raise ValueError("UniMem DSPy demonstrations require at most 64 bounded objects")
        predictors[name] = {"signature": {"instructions": instructions, "fields": clean_fields}, "demos": demos}
    if not 1 <= len(predictors) <= 16:
        raise ValueError("UniMem DSPy state requires 1..16 predictors")
    value = candidate({"base_artifact": base, "delta": _json_text({"predictors": predictors}),
                       "optimizer": "dspy", "claimed_validation_score": None, "parent": parent})
    return [{**value, "adapter": "dspy", "format_status": "verified serialization subset",
             "schema_commit": DSPY_SCHEMA_COMMIT, "delta_encoding": "replacement-predictors-json",
             "dropped_runtime_configuration": True, "unit_kind": "candidate"}]


def pareto(points: Sequence[Mapping[str, Any]]) -> list[str]:
    """Maximize randomized effect and minimize randomized harm, with support.

    All numeric coordinates are supplied by authenticated canonical reports in
    the I/O layer. Claimed scores are intentionally absent from this contract.
    This is an estimated frontier, without a simultaneous optimality guarantee.
    """
    if len(points) > 256:
        raise ValueError("UniMem Pareto frontier exceeds 256 candidates")
    clean = []
    for point in points:
        if set(point) != {"id", "effect", "harm", "supported"} or type(point["supported"]) is not bool:
            raise ValueError("UniMem invalid randomized frontier point")
        identity = _text(point["id"], "frontier ID", 256)
        effect, harm = point["effect"], point["harm"]
        for number in (effect, harm):
            if not finite_number(number):
                raise ValueError("UniMem frontier coordinates must be finite")
        if not -1 <= effect <= 1 or not 0 <= harm <= 1:
            raise ValueError("UniMem frontier coordinates are outside their range")
        if point["supported"]:
            clean.append((identity, float(effect), float(harm)))
    return sorted(identity for identity, effect, harm in clean
                  if not any(e >= effect and h <= harm and (e > effect or h < harm)
                             for other, e, h in clean if other != identity))
