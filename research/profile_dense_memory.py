"""Profile the dense query/cache path with deterministic local vectors.

This measures implementation overhead, not embedding quality or BEAM accuracy.
Run against two checkouts with OPENBLAS_NUM_THREADS=1 for a repeatable comparison.
"""
import argparse
import json
import os
import platform
import sqlite3
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", help="CommonTrace checkout to import")
    parser.add_argument("--passages", type=int, default=50000)
    parser.add_argument("--dimensions", type=int, default=768)
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()
    if args.passages < 1 or args.dimensions < 2 or args.runs < 1:
        parser.error("passages/runs must be positive and dimensions at least two")
    sys.path.insert(0, os.path.abspath(args.checkout))
    import numpy as np

    from commontrace.conversation import Store, embed

    class Encoder:
        tag = "local-scaling-profile"
        calls = 0
        rows = 0

        def vectors(self, items):
            self.calls += 1
            self.rows += len(items)
            values = np.zeros((len(items), args.dimensions), dtype=np.float32)
            positions = [int(body.split()[-1]) for _h, body in items]
            values[:, 0] = [i / (args.passages * 1.2) for i in positions]
            values[:, 1] = [i % 997 / 997 for i in positions]
            # Match the representation already persisted by the real Embedder.
            return values.astype(np.float16).astype(np.float32)

    with tempfile.TemporaryDirectory() as root, Store(root, "profile") as store:
        store.add("large", [{"text": f"passage {i}"} for i in range(args.passages)])
        encoder = Encoder()
        encoder.np = np
        query = np.zeros(args.dimensions, dtype=np.float32)
        query[0], query[1] = 0.8, 0.6
        times = []
        for _ in range(args.runs):
            start = time.perf_counter()
            hits = embed.search(store, encoder, query, 10)
            times.append(round((time.perf_counter() - start) * 1000, 3))
        indexes = list(embed._INDEX.values())
        print(json.dumps({
            "implementation": os.path.abspath(args.checkout), "python": platform.python_version(),
            "numpy": np.__version__, "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
            "passages": args.passages, "dimensions": args.dimensions, "search_ms": times,
            "vector_batches": encoder.calls, "vectors_read": encoder.rows,
            "cached_vector_bytes": sum(index.matrix.nbytes for index in indexes),
            "top_ids": [uid for uid, _score in hits], "model": "deterministic local synthetic vectors",
        }))


if __name__ == "__main__":
    main()
