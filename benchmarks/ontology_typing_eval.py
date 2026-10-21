"""Measure ontology typing of entity mentions on a labeled fixture.

Two views of the same labels:

- ``classifier``: `ontology_classify.classify(name, context)` for every row.
- ``pipeline``: `entities.extract(context, onto=...)` and the type the extractor
  gives the labeled mention (what the knowledge graph actually stores); a row whose
  mention is not extracted at all counts as untyped.

A prediction is correct when it is the expected type or one of the row's `accept`
types; for rows whose expected type is null, correct means no declared type was
given (the classifier abstained or the extractor kept the fallback type). Results
are reported overall and per slice: ``known-name`` rows may be helped by built-in
gazetteers, ``novel-name`` rows use invented names that only the mention's shape
and context can type, and ``abstract`` rows must stay untyped.

Run with ``python -m benchmarks.ontology_typing_eval --fixture FILE --json`` (add
``--methods keyword,embedding --embedder fastembed:BAAI/bge-small-en-v1.5`` or
``--llm`` to include the model tiers; those numbers depend on the model).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from typing import Any


def load_fixture(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    rows = data.get("examples")
    if data.get("schema_version") != "1.0.0" or not isinstance(rows, list) or not rows:
        raise ValueError("ontology typing fixture has an unsupported or empty schema")
    for row in rows:
        if not {"id", "name", "context", "expected", "slice"} <= set(row):
            raise ValueError(f"fixture row {row.get('id')!r} is incomplete")
    return rows


def _correct(row: dict[str, Any], predicted: str | None) -> bool:
    if row["expected"] is None:
        return predicted is None
    return predicted == row["expected"] or predicted in (row.get("accept") or [])


def score(rows: list[dict[str, Any]], predictions: list[str | None]) -> dict[str, Any]:
    """Accuracy overall and per slice and type, coverage, precision of typed answers, macro-F1."""
    by_slice: dict[str, list[bool]] = defaultdict(list)
    by_type: dict[str, list[bool]] = defaultdict(list)
    confusion: Counter = Counter()
    typed = typed_right = 0
    for row, got in zip(rows, predictions):
        ok = _correct(row, got)
        by_slice[row["slice"]].append(ok)
        by_type[row["expected"] or "none"].append(ok)
        if got is not None:
            typed += 1
            typed_right += ok
        if not ok:
            confusion[f"{row['expected'] or 'none'}->{got or 'none'}"] += 1
    labels = sorted({r["expected"] for r in rows if r["expected"]})
    f1s = []
    for label in labels:
        tp = sum(1 for r, g in zip(rows, predictions) if g == label and _correct(r, g))
        fp = sum(1 for r, g in zip(rows, predictions) if g == label and not _correct(r, g))
        fn = sum(1 for r, g in zip(rows, predictions) if r["expected"] == label and not _correct(r, g))
        p = tp / (tp + fp) if tp + fp else 0.0
        r_ = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * p * r_ / (p + r_) if p + r_ else 0.0)
    total = [ok for oks in by_slice.values() for ok in oks]
    return {
        "examples": len(rows),
        "accuracy": round(sum(total) / len(total), 4),
        "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
        "typed": typed,
        "typed_precision": round(typed_right / typed, 4) if typed else 0.0,
        "by_slice": {k: {"accuracy": round(sum(v) / len(v), 4), "examples": len(v)} for k, v in sorted(by_slice.items())},
        "by_type": {k: {"accuracy": round(sum(v) / len(v), 4), "examples": len(v)} for k, v in sorted(by_type.items())},
        "errors": dict(confusion.most_common()),
    }


def predict_classifier(rows, onto, **options) -> list[str | None]:
    from unimem import ontology_classify

    return [ontology_classify.classify(r["name"], r["context"], onto=onto, **options).type for r in rows]


def predict_pipeline(rows, onto, embedder=None) -> list[str | None]:
    from unimem import entities
    from unimem.ontology import FALLBACK_TYPE

    out: list[str | None] = []
    for row in rows:
        found = entities.extract(row["context"], onto=onto, use_spacy=False, embedder=embedder)
        name = row["name"].lower()
        match = next((m for m in found if m.name.lower() == name), None) or \
            next((m for m in found if name in m.name.lower() or m.name.lower() in name), None)
        type_ = match.type if match is not None else None
        out.append(None if type_ in (None, FALLBACK_TYPE) else type_)
    return out


def run(path: str, *, methods: tuple[str, ...] = ("keyword",), embedder=None,
        llm=None) -> dict[str, Any]:
    from unimem import ontology

    rows = load_fixture(path)
    onto = ontology.Ontology.default()
    started = time.perf_counter()
    classifier = predict_classifier(rows, onto, methods=methods, embedder=embedder, llm=llm)
    classifier_ms = (time.perf_counter() - started) * 1000 / len(rows)
    started = time.perf_counter()
    pipeline = predict_pipeline(rows, onto, embedder=embedder if "embedding" in methods else None)
    pipeline_ms = (time.perf_counter() - started) * 1000 / len(rows)
    return {
        "fixture": os.path.basename(path),
        "methods": list(methods),
        "embedder": embedder if isinstance(embedder, str) else None,
        "llm": llm is not None,
        "classifier": {**score(rows, classifier), "ms_per_mention": round(classifier_ms, 3)},
        "pipeline": {**score(rows, pipeline), "ms_per_mention": round(pipeline_ms, 3)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", required=True,
                        help="labeled mentions (JSON, schema_version 1.0.0); none ship with the repository")
    parser.add_argument("--methods", default="keyword", help="comma-separated classify methods")
    parser.add_argument("--embedder", default=None, help="embedding provider tag for the embedding tier")
    parser.add_argument("--llm", action="store_true", help="use the configured LLM for the llm tier")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    llm = None
    if args.llm:
        from unimem import llm as llm_mod

        llm = llm_mod.complete
    methods = tuple(m.strip() for m in args.methods.split(",") if m.strip())
    report = run(args.fixture, methods=methods, embedder=args.embedder, llm=llm)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    for view in ("classifier", "pipeline"):
        part = report[view]
        slices = ", ".join(f"{k} {v['accuracy']:.2f}" for k, v in part["by_slice"].items())
        print(f"{view:10s} accuracy {part['accuracy']:.3f}  macro-F1 {part['macro_f1']:.3f}  "
              f"typed precision {part['typed_precision']:.3f}  ({slices})  {part['ms_per_mention']} ms/mention")
    return 0


if __name__ == "__main__":
    sys.exit(main())
