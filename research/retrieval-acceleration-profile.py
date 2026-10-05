"""Measure local exact search and scoped cold mapped-cache reads without model APIs."""
import argparse
import gc
import json
import os
import platform
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout")
    parser.add_argument("--passages", type=int, default=50000)
    parser.add_argument("--dimensions", type=int, default=768)
    parser.add_argument("--queries", type=int, default=8)
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--scoped", action="store_true")
    parser.add_argument("--mapped", action="store_true")
    args = parser.parse_args()
    if min(args.passages, args.dimensions, args.queries, args.limit, args.runs) < 1 or args.passages < 20:
        parser.error("positive arguments and at least 20 passages are required")
    sys.path.insert(0, os.path.abspath(args.checkout))
    import numpy as np

    from commontrace.conversation import Store, embed, vector_index

    if args.mapped or args.scoped:
        embed.MAX_INDEX_BYTES = 1
    encoded = 0

    def synthetic(texts, query=False):
        nonlocal encoded
        encoded += len(texts)
        positions = np.array([int(text.split()[-1]) for text in texts], dtype=np.float32)
        phases = np.arange(1, args.dimensions + 1, dtype=np.float32)
        values = np.sin(positions[:, None] * phases[None, :] * 0.013 + phases)
        values /= np.linalg.norm(values, axis=1)[:, None]
        return values

    with tempfile.TemporaryDirectory() as root, Store(root, "acceleration") as store:
        store.add("needle", [{"text": f"passage {i}"} for i in range(10)])
        store.add("haystack", [{"text": f"passage {i}"} for i in range(10, args.passages)])
        encoder = embed.Embedder(root, "minilm")
        encoder.encode = synthetic
        queries = np.random.default_rng(2026).normal(size=(args.queries, args.dimensions)).astype(np.float32)
        queries /= np.linalg.norm(queries, axis=1)[:, None]
        embed.search_many(store, encoder, queries, args.limit)
        allowed = {t.id for t in store.session_turns("needle")} if args.scoped else None
        path = vector_index.path_for(store, encoder.tag)
        dropped = False
        times, pages = [], None
        encoded = 0
        for _ in range(args.runs):
            if args.scoped:
                embed.forget_store(store)
                gc.collect()
                if hasattr(os, "posix_fadvise") and hasattr(os, "POSIX_FADV_DONTNEED"):
                    with open(path, "rb") as file:
                        os.posix_fadvise(file.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                    dropped = True
            start = time.perf_counter()
            pages = embed.search_many(store, encoder, queries, args.limit, allowed=allowed)
            times.append(round((time.perf_counter() - start) * 1000, 3))
        print(json.dumps({"python": platform.python_version(), "numpy": np.__version__,
                          "passages": args.passages, "dimensions": args.dimensions, "queries": args.queries,
                          "limit": args.limit, "scoped": args.scoped,
                          "eligible_passages": 10 if args.scoped else args.passages,
                          "mapped": args.mapped or args.scoped, "search_ms": times, "encoded_during_timing": encoded,
                          "advised_drop_cache_pages": dropped, "seed": 2026,
                          "snapshot_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
                          "top_ids": [[uid for uid, _score in page] for page in pages],
                          "scores": [[score for _uid, score in page] for page in pages]}))
        encoder.close()


if __name__ == "__main__":
    main()
