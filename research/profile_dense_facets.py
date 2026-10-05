"""Measure exact multi-query retrieval with deterministic local vectors; no model or network."""
import argparse
import json
import os
import platform
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkout')
    parser.add_argument('--passages', type=int, default=50000)
    parser.add_argument('--dimensions', type=int, default=768)
    parser.add_argument('--queries', type=int, default=8)
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--persistent', action='store_true',
                        help='Use the real SQLite vector cache with synthetic encoding')
    args = parser.parse_args()
    if min(args.passages, args.dimensions, args.queries, args.runs) < 1:
        parser.error('passages, dimensions, queries and runs must be positive')
    sys.path.insert(0, os.path.abspath(args.checkout))
    import numpy as np

    from commontrace.conversation import Store, embed

    class Encoder:
        tag = 'facet-profile'
        rows = 0
        def vectors(self, items):
            self.rows += len(items)
            positions = np.array([int(text.split()[-1]) for _hash, text in items], dtype=np.float32)
            phases = np.arange(1, args.dimensions + 1, dtype=np.float32)
            values = np.sin(positions[:, None] * phases[None, :] * 0.013 + phases)
            values /= np.linalg.norm(values, axis=1)[:, None]
            return values.astype(np.float16).astype(np.float32)

    with tempfile.TemporaryDirectory() as root, Store(root, 'facets') as store:
        store.add('large', [{'text': f'passage {i}'} for i in range(args.passages)])
        synthetic = Encoder()
        synthetic.np = np
        encoder = synthetic
        if args.persistent:
            encoder = embed.Embedder(root, 'minilm')
            encoder.encode = lambda texts, query=False: synthetic.vectors([('', text) for text in texts])
        queries = np.random.default_rng(2026).normal(size=(args.queries, args.dimensions)).astype(np.float32)
        queries /= np.linalg.norm(queries, axis=1)[:, None]
        embed.search(store, encoder, queries[0], 10)
        times = []
        for _ in range(args.runs):
            start = time.perf_counter()
            if hasattr(embed, 'search_many'):
                results = embed.search_many(store, encoder, queries, 10)
            else:
                results = [embed.search(store, encoder, q, 10) for q in queries]
            times.append(round((time.perf_counter() - start) * 1000, 3))
        print(json.dumps({'python': platform.python_version(), 'numpy': np.__version__,
                          'passages': args.passages, 'dimensions': args.dimensions, 'queries': args.queries,
                          'search_ms': times, 'vectors_encoded': synthetic.rows, 'persistent_cache': args.persistent,
                          'top_ids': [[uid for uid, _score in hits] for hits in results],
                          'model': 'deterministic normalized sine vectors, seed 2026'}))
        if args.persistent:
            encoder.close()


if __name__ == '__main__':
    main()
