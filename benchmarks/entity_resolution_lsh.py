"""Compare exhaustive and lossless MinHash-band entity candidate generation.

This synthetic diagnostic verifies exact output parity while measuring only the
candidate-generation and existing acceptance predicates. It does not measure
graph I/O, embeddings, LLM review, or production entity-resolution quality.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import statistics
import subprocess
import sys
import time
from itertools import combinations
from pathlib import Path

from unimem import entity_resolution, kg_similarity
from unimem.overlap import estimate_jaccard

ENTROPY_THRESHOLD = 2.0
MINHASH_THRESHOLD = 0.8
JACCARD_THRESHOLD = 0.8


def _surfaces(size: int) -> list:
    rng = random.Random(31)
    vocabulary = [f"w{i:04d}x" for i in range(1200)]
    names = []
    for _ in range(size // 2):
        words = rng.sample(vocabulary, 5)
        names.extend((" ".join(words), " ".join([*words[:4], words[4] + "s"])))
    if len(names) < size:
        names.append(" ".join(rng.sample(vocabulary, 5)))
    return [entity_resolution._tiered_surface([name]) for name in names[:size]]


def _exhaustive(surfaces: list) -> set[tuple[int, int]]:
    accepted = set()
    for a, b in combinations(range(len(surfaces)), 2):
        left, right = surfaces[a], surfaces[b]
        if left.forms & right.forms:
            accepted.add((a, b))
            continue
        if min(left.entropy, right.entropy) < ENTROPY_THRESHOLD:
            continue
        similarity = estimate_jaccard(left.signature, right.signature)
        if similarity >= MINHASH_THRESHOLD and kg_similarity.jaccard(left.grams, right.grams) >= JACCARD_THRESHOLD:
            accepted.add((a, b))
    return accepted


def _indexed(surfaces: list) -> set[tuple[int, int]]:
    accepted = set(entity_resolution._tiered_exact_pairs(surfaces))
    for a, b in entity_resolution._tiered_minhash_pairs(
        surfaces, entropy_threshold=ENTROPY_THRESHOLD, minhash_threshold=MINHASH_THRESHOLD
    ):
        if (a, b) in accepted:
            continue
        similarity = estimate_jaccard(surfaces[a].signature, surfaces[b].signature)
        if similarity >= MINHASH_THRESHOLD and kg_similarity.jaccard(surfaces[a].grams, surfaces[b].grams) >= JACCARD_THRESHOLD:
            accepted.add((a, b))
    return accepted


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _head() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def measure(size: int, repeats: int) -> dict:
    surfaces = _surfaces(size)
    samples = {"exhaustive": [], "indexed": []}
    expected = None
    # Alternate an ABBA block to reduce order and warm-up bias.
    order = ("exhaustive", "indexed", "indexed", "exhaustive") * repeats
    for mode in order:
        started = time.perf_counter_ns()
        pairs = _exhaustive(surfaces) if mode == "exhaustive" else _indexed(surfaces)
        samples[mode].append((time.perf_counter_ns() - started) / 1_000_000)
        if expected is None:
            expected = pairs
        elif pairs != expected:
            raise RuntimeError(f"candidate output mismatch at {size} entities ({mode})")
    possible = size * (size - 1) // 2
    indexed_candidates = entity_resolution._tiered_minhash_pairs(
        surfaces, entropy_threshold=ENTROPY_THRESHOLD, minhash_threshold=MINHASH_THRESHOLD
    )
    return {
        "entities": size,
        "possible_pairs": possible,
        "indexed_minhash_candidates": len(indexed_candidates),
        "accepted_pairs": len(expected or ()),
        "candidate_fraction": len(indexed_candidates) / possible if possible else 0.0,
        "median_ms": {mode: statistics.median(values) for mode, values in samples.items()},
        "samples_ms": samples,
        "exact_output_parity": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[200, 700])
    parser.add_argument("--repeats", type=int, default=2, help="ABBA blocks per size (default: 2)")
    parser.add_argument("--output", help="Write JSON report to this path")
    args = parser.parse_args()
    if args.repeats < 1 or any(size < 2 or size > 2000 for size in args.sizes):
        parser.error("repeats must be positive and sizes must be in 2..2000")

    root = Path(__file__).resolve().parents[1]
    source_files = (root / "unimem/entity_resolution.py", root / "unimem/overlap.py",
                    root / "unimem/kg_similarity.py")
    report = {
        "measurement": "synthetic entity candidate-generation diagnostic",
        "command": "python -m benchmarks.entity_resolution_lsh " + " ".join(sys.argv[1:]),
        "source_commit": _head(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "seed": 31,
        "thresholds": {
            "entropy": ENTROPY_THRESHOLD,
            "minhash": MINHASH_THRESHOLD,
            "jaccard": JACCARD_THRESHOLD,
        },
        "source_sha256": {path.relative_to(root).as_posix(): _sha256(path) for path in source_files},
        "limitations": [
            "Synthetic five-token names are not a representative production entity corpus.",
            "The timing excludes graph loading, ontology checks, embeddings, and LLM judgment.",
            "Exact output parity here does not establish entity-resolution quality.",
        ],
        "results": [measure(size, args.repeats) for size in args.sizes],
    }
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
