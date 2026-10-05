"""Check prepared versus cold local recall. Use cached model weights and HF_HUB_OFFLINE=1."""
import argparse
import json
import sys
import tempfile
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('checkout')
    p.add_argument('--prepare', action='store_true')
    a = p.parse_args()
    sys.path.insert(0, a.checkout)
    from commontrace.conversation import Options, Store, embed, recall
    with tempfile.TemporaryDirectory() as root, Store(root, 'local') as store:
        store.add('archive', [{'text': f'Reference item {i}: project unit {i} uses a scheduled review.'}
                              for i in range(256)])
        store.add('old', [{'speaker': 'Ana', 'text': 'I live in London.'}], session_at='2023-01-01')
        store.add('new', [{'speaker': 'Ana', 'text': 'I live in Paris.'}], session_at='2025-01-01')
        store.add('project', [{'text': 'My colleague Mira leads Project Zephyr.'}])
        store.add('award', [{'text': 'Mira won the Polaris Prize.'}])
        encoder = embed.Embedder(root, 'arctic-m')
        start = time.perf_counter()
        encoder.encode(['warmup'], query=True)
        load_s = time.perf_counter() - start
        prepared = embed.prepare(store, encoder) if a.prepare else None
        start = time.perf_counter()
        result = recall(store, 'What award did my colleague who leads Project Zephyr receive?',
                        options=Options(embedder='arctic-m', rerank=None))
        recall_ms = (time.perf_counter() - start) * 1000
        historical = recall(store, 'Where does Ana live?', now='2024-01-01',
                            options=Options(embedder='arctic-m', rerank=None))
        print(json.dumps({'model': 'Snowflake/snowflake-arctic-embed-m-v1.5', 'inference': 'local CPU, offline',
                          'model_load_seconds': load_s, 'prepared': prepared, 'first_recall_ms': recall_ms,
                          'tokens': result.tokens, 'bridge_found': 'Polaris' in result.context,
                          'historical_found': 'London' in historical.context and 'Paris' not in historical.context}))
        encoder.close()


if __name__ == "__main__":
    main()
