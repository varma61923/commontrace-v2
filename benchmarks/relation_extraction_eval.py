"""Measure relation (triple) extraction on a labeled fixture.

Each row is a sentence with its expected triples (``[]`` for sentences that name two
entities without asserting a relation: negated, hedged or planned). A predicted triple
is correct when its relation matches and its subject and object match the labels:

- ``strict``: equal after case-folding and dropping a leading article;
- ``lenient``: one name contains the other ("Payments Service" for "Payments").

Every triple extracted from a negative sentence is a false positive. Results are
reported overall and per relation.

Run with ``python -m benchmarks.relation_extraction_eval --fixture FILE [--llm]``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from typing import Any


def load_fixture(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("examples")
    if data.get("schema_version") != "1.0.0" or not isinstance(rows, list) or not rows:
        raise ValueError("relation fixture has an unsupported or empty schema")
    return rows


def _norm(name: str) -> str:
    text = re.sub(r"^(?:the|a|an|our|their)\s+", "", (name or "").strip().casefold())
    return re.sub(r"[\s`'\".,]+", " ", text).strip()


def _same(a: str, b: str, lenient: bool) -> bool:
    a, b = _norm(a), _norm(b)
    return a == b or (lenient and bool(a) and bool(b) and (a in b or b in a))


def score(rows: list[dict[str, Any]], predictions: list[list[tuple[str, str, str]]], *, lenient: bool) -> dict:
    tp = fp = fn = 0
    per: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    negatives_clean = negatives = 0
    for row, got in zip(rows, predictions):
        gold = [(t["subject"], t["relation"], t["object"]) for t in row["triples"]]
        if not gold:
            negatives += 1
            negatives_clean += not got
        used: set[int] = set()
        for s, r, o in got:
            match = next((i for i, (gs, gr, go) in enumerate(gold) if i not in used and gr == r
                          and _same(gs, s, lenient) and _same(go, o, lenient)), None)
            if match is None:
                fp += 1
                per[r][1] += 1
            else:
                used.add(match)
                tp += 1
                per[r][0] += 1
        for i, (_gs, gr, _go) in enumerate(gold):
            if i not in used:
                fn += 1
                per[gr][2] += 1
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0,
            "tp": tp, "fp": fp, "fn": fn,
            "negatives_without_triples": f"{negatives_clean}/{negatives}",
            "by_relation": {k: {"tp": v[0], "fp": v[1], "fn": v[2]} for k, v in sorted(per.items())}}


def run(path: str, *, llm=None) -> dict[str, Any]:
    from commontrace import ontology, relation_extraction

    rows = load_fixture(path)
    onto = ontology.Ontology.default()
    started = time.perf_counter()
    predictions = []
    for row in rows:
        triples = relation_extraction.extract(row["text"], onto, llm=llm)
        predictions.append([(t.subject, t.relation, t.object) for t in triples])
    ms = (time.perf_counter() - started) * 1000 / len(rows)
    return {"rows": len(rows), "llm": llm is not None, "ms_per_sentence": round(ms, 3),
            "strict": score(rows, predictions, lenient=False), "lenient": score(rows, predictions, lenient=True),
            "predictions": predictions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--llm", action="store_true", help="extract with the configured LLM")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    llm = None
    if args.llm:
        from commontrace import llm as llm_mod

        llm_mod.load_config()
        llm = llm_mod.complete
    report = run(args.fixture, llm=llm)
    report.pop("predictions")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    for view in ("strict", "lenient"):
        part = report[view]
        print(f"{view:8s} P {part['precision']:.3f}  R {part['recall']:.3f}  F1 {part['f1']:.3f}  "
              f"negatives clean {part['negatives_without_triples']}  ({report['ms_per_sentence']} ms/sentence)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
