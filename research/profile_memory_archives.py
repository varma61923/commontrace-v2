"""Profile portable archive allocation peaks with a synthetic multi-session corpus."""
import argparse
import contextlib
import io
import json
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument('checkout')
    a = p.parse_args()
    sys.path.insert(0, a.checkout)
    from commontrace.cli import main
    from commontrace.conversation import Store
    with tempfile.TemporaryDirectory() as root:
        output = str(Path(root) / 'archive.jsonl')
        with Store(root, 'source') as store:
            for session in range(300):
                store.add(f's{session}', [{'text': f'Entry {session}.{i}: ' + 'technical reference detail ' * 20}
                                         for i in range(20)])
        measurements = {}
        for operation, args in [('export', ['conversation', 'export', 'source', '--out', output, '--dest', root]),
                                ('import', ['conversation', 'import', 'copy', output, '--dest', root])]:
            tracemalloc.start()
            start = time.perf_counter()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                assert main(args) == 0
            elapsed = time.perf_counter() - start
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            measurements[operation] = {'seconds': elapsed, 'peak_python_bytes': peak}
        with Store(root, 'copy') as store:
            assert store.stats()['turns'] == 6000
        print(json.dumps({'sessions': 300, 'messages': 6000, 'archive_bytes':Path(output).stat().st_size,
                          'measurement':'tracemalloc Python allocation peak; excludes SQLite/native allocations',
                          'measurements':measurements}))


if __name__ == "__main__":
    main()
