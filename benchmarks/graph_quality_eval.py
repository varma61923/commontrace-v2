"""Run deterministic labeled checks for graph resolution, typing and extraction.

The checked-in corpus is a curated synthetic fixture, not a sample of production
traffic. It measures exact/MinHash entity candidates, keyword ontology typing,
and offline relation patterns. Embedding and LLM tiers are deliberately marked
unmeasured unless a separately labeled, model-backed corpus is supplied.

Run with ``python -m benchmarks.graph_quality_eval --json``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from typing import Any

from commontrace import entity_resolution, ontology, ontology_classify, relation_extraction

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FIXTURE = os.path.join(_HERE, "fixtures", "graph_quality_v1.json")
SCHEMA_VERSION = "1.0.0"
ENTROPY_SWEEP = (1.0, 1.5, 2.0, 2.5, 3.0)
SIMILARITY_SWEEP = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
CLASSIFICATION_SWEEP = (0.45, 0.6, 0.7, 0.75, 0.9, 0.95)
LEGACY_NGRAM_SWEEP = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 1.0)


def load_fixture(path: str = DEFAULT_FIXTURE) -> dict[str, Any]:
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    required = {"entity_pairs", "classifications", "relation_extraction"}
    if data.get("schema_version") != SCHEMA_VERSION or not required <= set(data):
        raise ValueError("graph quality fixture has an unsupported or incomplete schema")
    for key in required:
        if not isinstance(data[key], list):
            raise ValueError(f"graph quality fixture field {key!r} must be a list")
    if not data["entity_pairs"] or not data["classifications"] or not data["relation_extraction"]:
        raise ValueError("each graph quality fixture task needs at least one labeled example")
    return data


def binary_metrics(expected: list[bool], predicted: list[bool]) -> dict[str, Any]:
    if len(expected) != len(predicted):
        raise ValueError("expected and predicted labels must have equal length")
    tp = sum(want and got for want, got in zip(expected, predicted))
    fp = sum(not want and got for want, got in zip(expected, predicted))
    fn = sum(want and not got for want, got in zip(expected, predicted))
    tn = sum(not want and not got for want, got in zip(expected, predicted))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "examples": len(expected),
        "positive_labels": tp + fn,
        "predicted_positive": tp + fp,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
    }


def _entity_signals(rows: list[dict]) -> list[dict[str, Any]]:
    return [
        {
            "id": row["id"],
            "duplicate": bool(row["duplicate"]),
            "stratum": row["stratum"],
            **entity_resolution.tiered_pair_signals(
                [row["a"]["name"], *row["a"].get("aliases", [])],
                [row["b"]["name"], *row["b"].get("aliases", [])],
            ),
        }
        for row in rows
    ]


def _resolve_signals(signals: list[dict[str, Any]], *, entropy: float, minhash: float, jaccard: float) -> list[bool]:
    return [bool(row["exact"] or (
        row["entropy"] >= entropy and row["minhash"] >= minhash and row["jaccard"] >= jaccard
    )) for row in signals]


def _entity_report(rows: list[dict]) -> dict[str, Any]:
    signals = _entity_signals(rows)
    labels = [row["duplicate"] for row in signals]
    defaults = entity_resolution.TIERED_THRESHOLDS
    predictions = _resolve_signals(
        signals, entropy=defaults["entropy"], minhash=defaults["minhash"], jaccard=defaults["jaccard"],
    )
    base = binary_metrics(labels, predictions)
    errors = []
    for row, predicted in zip(signals, predictions):
        if predicted != row["duplicate"]:
            errors.append({key: row[key] for key in ("id", "duplicate", "stratum", "exact", "entropy",
                                                       "minhash", "jaccard")})
    strata = {}
    for stratum in sorted({row["stratum"] for row in signals}):
        subset = [row for row in signals if row["stratum"] == stratum]
        strata[stratum] = binary_metrics([row["duplicate"] for row in subset], _resolve_signals(
            subset, entropy=defaults["entropy"], minhash=defaults["minhash"], jaccard=defaults["jaccard"],
        ))

    def sweep(name: str, values: tuple[float, ...]) -> dict[str, dict]:
        out = {}
        for value in values:
            params = {"entropy": defaults["entropy"], "minhash": defaults["minhash"],
                      "jaccard": defaults["jaccard"]}
            params[name] = value
            out[f"{value:.2f}"] = binary_metrics(labels, _resolve_signals(
                signals, entropy=params["entropy"], minhash=params["minhash"], jaccard=params["jaccard"],
            ))
        return out

    return {
        "scope": "exact+MinHash/entropy only",
        "default_thresholds": dict(defaults),
        "metrics": base,
        "misclassified_pairs": errors,
        "by_stratum": strata,
        "threshold_sweep": {
            "entropy": sweep("entropy", ENTROPY_SWEEP),
            "minhash": sweep("minhash", SIMILARITY_SWEEP),
            "jaccard": sweep("jaccard", SIMILARITY_SWEEP),
        },
        "embedding": "unmeasured",
        "llm": "unmeasured",
        "notes": [
            "Pair labels are hand-authored synthetic fixtures; rates are fixture scores, not production estimates.",
            "Cosine and LLM decision thresholds cannot be tuned from this corpus because no model scores or judgments are labeled.",
        ],
    }


def _legacy_ngram_report(rows: list[dict]) -> dict[str, Any]:
    """Score fixture pairs with the legacy resolver's best-form trigram Jaccard."""
    from types import SimpleNamespace

    scores = []
    for row in rows:
        nodes = [
            SimpleNamespace(
                id=identity,
                name=entity["name"],
                properties={"aliases": entity.get("aliases", [])},
            )
            for identity, entity in (("a", row["a"]), ("b", row["b"]))
        ]
        score = entity_resolution._ngram_scores(nodes).get((0, 1), 0.0)
        scores.append({"id": row["id"], "duplicate": bool(row["duplicate"]), "score": score})

    labels = [row["duplicate"] for row in scores]

    def metrics_at(threshold: float) -> dict[str, Any]:
        return binary_metrics(labels, [row["score"] >= threshold for row in scores])

    report_threshold, apply_threshold = entity_resolution.THRESHOLDS["ngram"]
    return {
        "scope": "offline best-form trigram Jaccard from the legacy resolver; paired fixture only",
        "thresholds": {"report": report_threshold, "apply": apply_threshold},
        "report_threshold_metrics": metrics_at(report_threshold),
        "apply_threshold_metrics": metrics_at(apply_threshold),
        "threshold_sweep": {f"{value:.2f}": metrics_at(value) for value in LEGACY_NGRAM_SWEEP},
        "pair_scores": scores,
        "notes": [
            "Scores use the legacy resolver's trigram Jaccard; the fixture does not measure embedding mode.",
            "The apply score floor is evaluated on labels only; ontology compatibility and graph-level merge behavior are not represented.",
            "Hand-authored synthetic pair labels are not production estimates and do not justify retuning defaults.",
        ],
    }


def _classification_metrics(rows: list[dict], threshold: float) -> dict[str, Any]:
    onto = ontology.Ontology.default()
    expected = [row["expected"] for row in rows]
    predicted = [ontology_classify.classify(
        row["name"], row.get("context", ""), onto=onto, threshold=threshold, methods=("keyword",),
    ).type for row in rows]
    true_positive = sum(want is not None and got == want for want, got in zip(expected, predicted))
    false_positive = sum(got is not None and got != want for want, got in zip(expected, predicted))
    false_negative = sum(want is not None and got != want for want, got in zip(expected, predicted))
    true_negative = sum(want is None and got is None for want, got in zip(expected, predicted))
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: {"examples": 0, "correct": 0})
    for want, got in zip(expected, predicted):
        key = want or "unknown"
        by_type[key]["examples"] += 1
        by_type[key]["correct"] += int(got == want)
    return {
        "examples": len(rows),
        "positive_labels": sum(want is not None for want in expected),
        "predicted_positive": sum(got is not None for got in predicted),
        "tp": true_positive,
        "fp": false_positive,
        "fn": false_negative,
        "tn": true_negative,
        "accuracy": round(sum(want == got for want, got in zip(expected, predicted)) / len(rows), 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
        "by_expected_type": {
            key: {**value, "accuracy": round(value["correct"] / value["examples"], 4)}
            for key, value in sorted(by_type.items())
        },
    }


def _classification_report(rows: list[dict]) -> dict[str, Any]:
    return {
        "methods": ["keyword"],
        "thresholds": {"keyword": ontology_classify.THRESHOLD,
                        "embedding": ontology_classify.EMBEDDING_THRESHOLD},
        "default_threshold": ontology_classify.THRESHOLD,
        "metrics": _classification_metrics(rows, ontology_classify.THRESHOLD),
        "threshold_sweep": {
            f"{threshold:.2f}": _classification_metrics(rows, threshold)
            for threshold in CLASSIFICATION_SWEEP
        },
        "embedding": "unmeasured",
        "llm": "unmeasured",
        "notes": ["Labels are hand-authored synthetic fixtures; no embedding or model-based types are judged."],
    }


def _relation_report(rows: list[dict]) -> dict[str, Any]:
    onto = ontology.Ontology.default()
    tp = fp = fn = 0
    by_relation: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for row in rows:
        expected = {(item["subject"].casefold(), item["relation"], item["object"].casefold())
                    for item in row["triples"]}
        predicted = {(item.subject.casefold(), item.relation, item.object.casefold())
                     for item in relation_extraction.extract_patterns(row["text"], onto)}
        for item in predicted - expected:
            fp += 1
            by_relation[item[1]]["fp"] += 1
        for item in expected - predicted:
            fn += 1
            by_relation[item[1]]["fn"] += 1
        for item in predicted & expected:
            tp += 1
            by_relation[item[1]]["tp"] += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "method": "pattern",
        "examples": len(rows),
        "gold_triples": tp + fn,
        "predicted_triples": tp + fp,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
        "by_relation": {key: value for key, value in sorted(by_relation.items())},
        "llm": "unmeasured",
        "notes": ["Exact triple match on hand-authored synthetic sentences; this is not a natural-language benchmark."],
    }


def evaluate(fixture: str = DEFAULT_FIXTURE) -> dict[str, Any]:
    data = load_fixture(fixture)
    with open(fixture, "rb") as stream:
        fixture_sha256 = hashlib.sha256(stream.read()).hexdigest()
    with open(__file__, "rb") as stream:
        harness_sha256 = hashlib.sha256(stream.read()).hexdigest()
    implementations = {}
    for name in ("entity_resolution.py", "ontology_classify.py", "relation_extraction.py"):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "commontrace", name)
        with open(path, "rb") as stream:
            implementations[f"commontrace/{name}"] = hashlib.sha256(stream.read()).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "fixture": os.path.relpath(fixture, _HERE) if os.path.isabs(fixture) else fixture,
        "fixture_sha256": fixture_sha256,
        "harness_sha256": harness_sha256,
        "implementation_sha256": implementations,
        "fixture_source": data.get("source", "unspecified"),
        "legacy_ngram_resolution": _legacy_ngram_report(data["entity_pairs"]),
        "entity_resolution": _entity_report(data["entity_pairs"]),
        "classification": _classification_report(data["classifications"]),
        "relation_extraction": _relation_report(data["relation_extraction"]),
        "threshold_decisions": {
            "legacy_ngram_report_and_apply": "preserved; fixture is too small and synthetic for production retuning",
            "entity_entropy_minhash_jaccard": "preserved; fixture is too small and synthetic for production retuning",
            "entity_embedding_cosine_and_top_k": "preserved; no labeled model scores or judged top-k candidates",
            "classification_embedding_cosine": "preserved; no labeled model scores",
            "classification_keyword": "preserved; fixture is too small and synthetic for production retuning",
        },
    }


def render(report: dict[str, Any]) -> str:
    er, legacy, classification, relation = (report["entity_resolution"], report["legacy_ngram_resolution"],
                                           report["classification"], report["relation_extraction"])
    lines = [
        "# Graph quality fixture evaluation",
        "",
        f"Fixture: `{report['fixture']}` ({report['fixture_source']}).",
        "All labels are hand-authored synthetic examples; these numbers are not production estimates.",
        "",
        "| Task | Examples | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, metrics in (("Legacy n-gram at report threshold", legacy["report_threshold_metrics"]),
                           ("Legacy n-gram at apply threshold", legacy["apply_threshold_metrics"]),
                           ("Exact + MinHash entity candidates", er["metrics"]),
                           ("Keyword ontology classification", classification["metrics"]),
                           ("Offline relation patterns", relation)):
        lines.append(f"| {label} | {metrics['examples']} | {metrics['tp']} | {metrics['fp']} | {metrics['fn']} | "
                     f"{metrics['precision']:.4f} | {metrics['recall']:.4f} | {metrics['f1']:.4f} |")
    lines.extend([
        "",
        "Embedding and LLM tiers are marked unmeasured. The entity-resolution and classification sweeps are diagnostic;",
        "no production threshold was changed because this synthetic fixture does not establish generalization.",
        "",
        "Legacy n-gram thresholds (report 0.60, apply 0.85) are evaluated separately from the opt-in tiered cascade.",
        "Apply-floor scores evaluate labeled pairs only; they do not represent ontology gates or graph merges.",
        "",
        "Entity resolution threshold sweep (one threshold at a time; all others at default):",
        "",
        "| Signal | Threshold | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for signal, rows in er["threshold_sweep"].items():
        for value, metrics in rows.items():
            lines.append(f"| {signal} | {value} | {metrics['tp']} | {metrics['fp']} | {metrics['fn']} | "
                         f"{metrics['precision']:.4f} | {metrics['recall']:.4f} | {metrics['f1']:.4f} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", default=DEFAULT_FIXTURE)
    parser.add_argument("--json", action="store_true", help="emit reproducible JSON instead of a Markdown summary")
    parser.add_argument("--out", help="write the report to this file instead of stdout")
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.fixture)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"graph quality evaluation failed: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.json else render(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as stream:
            stream.write(rendered)
        print(args.out)
    else:
        print(rendered, end="" if rendered.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
