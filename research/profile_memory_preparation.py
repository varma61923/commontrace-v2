"""Profile overlapping cold requests with synthetic vectors, without network or model inference."""
import argparse
import concurrent.futures
import json
import os
import sys
import tempfile
import threading
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('checkout')
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--passages', type=int, default=2048)
    a = p.parse_args()
    sys.path.insert(0, os.path.abspath(a.checkout))
    import numpy as np

    from commontrace.conversation import Store, embed

    counter_lock = threading.Lock()
    counter = {'calls': 0, 'rows': 0}
    def encode(self, texts, query=False):
        with counter_lock:
            counter['calls'] += 1
            counter['rows'] += len(texts)
        time.sleep(0.02)  # simulated latency makes concurrent cache misses overlap
        values = np.zeros((len(texts), 32), dtype=np.float32)
        values[:, 0] = [int(t.split()[-1]) / a.passages for t in texts]
        values[:, 1] = 1
        return values
    embed.Embedder.encode = encode
    with tempfile.TemporaryDirectory() as root:
        with Store(root, 'memory') as store:
            store.add('s', [{'text': f'passage {i}'} for i in range(a.passages)])
        stores = [Store(root, 'memory', create=False) for _ in range(a.workers)]
        encoders = [embed.Embedder(root, 'minilm') for _ in stores]
        barrier = threading.Barrier(a.workers)
        q = np.zeros(32, dtype=np.float32)
        q[0] = 1
        def search(i):
            barrier.wait()
            start = time.perf_counter()
            hits = embed.search(stores[i], encoders[i], q, 10)
            return {'ms': round((time.perf_counter() - start) * 1000, 3), 'ids': [h[0] for h in hits]}
        with concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
            results = list(pool.map(search, range(a.workers)))
        out = {'workers': a.workers, 'passages': a.passages, 'dimensions': 32,
               'model': 'deterministic synthetic encoder; 20ms artificial latency per batch',
               'encoder_calls': counter['calls'], 'encoded_rows': counter['rows'], 'queries': results}
        for store, encoder in zip(stores, encoders):
            store.close()
            encoder.close()
        print(json.dumps(out))


if __name__ == "__main__":
    main()
