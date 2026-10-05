"""Measure uncached recall results across request-scoped connections with a cached local model."""
import argparse
import json
import os
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkout')
    parser.add_argument('--runs', type=int, default=4)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error('runs must be positive')
    sys.path.insert(0, os.path.abspath(args.checkout))
    from commontrace.conversation import Options, Store, embed, recall
    with tempfile.TemporaryDirectory() as root:
        with Store(root, 'serving') as store:
            store.add('archive', [{'text': f'Reference item {i}: project unit {i} uses a scheduled review.'}
                                  for i in range(256)])
            facts = [
                ('venue', 'Project Zephyr venue is Oslo.'),
                ('budget', 'Project Zephyr budget is 120 units.'),
                ('color', 'Project Zephyr color is sky blue.'),
                ('launch', 'Project Zephyr launch is in November.'),
            ]
            for name, text in facts:
                store.add(name, [{'text': text}])
            encoder = embed.Embedder(root, 'arctic-m')
            model = embed._model('arctic-m')
            encoder.encode(['warmup'], query=True)
            prepared = embed.prepare(store, encoder)
            encoder.close()
        calls = []
        compute = model.encode
        def counted(texts, **kwargs):
            calls.append(len(texts))
            return compute(texts, **kwargs)
        model.encode = counted
        question = 'Summarize Project Zephyr, including venue, budget, color and launch.'
        opts = Options(embedder='arctic-m', rerank=None, graph_hops=0, neighbours_before=0, neighbours_after=0)
        measurements = []
        for _ in range(args.runs):
            start_calls = len(calls)
            with Store(root, 'serving', create=False) as request:
                start = time.perf_counter()
                result = recall(request, question, options=opts)
                elapsed = (time.perf_counter() - start) * 1000
                measurements.append({'ms': elapsed, 'encoder_calls': len(calls) - start_calls,
                                     'facets': len(result.explain['subqueries']), 'tokens': result.tokens,
                                     'coverage': all(t in result.context for t in
                                                     ['Oslo', '120 units', 'sky blue', 'November'])})
        print(json.dumps({'model': 'Snowflake/snowflake-arctic-embed-m-v1.5', 'inference': 'local CPU, offline',
                          'prepared': prepared, 'request_scoped_recall': measurements, 'encoder_batch_sizes': calls}))


if __name__ == '__main__':
    main()
