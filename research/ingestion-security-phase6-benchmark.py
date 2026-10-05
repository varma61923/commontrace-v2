"""Local-only frozen-source ingestion benchmark; run from the repository root."""

from __future__ import annotations

import cProfile
import io
import json
import pathlib
import platform
import pstats
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASELINE = "c8ce5c792916c10e23b0618c59259960294b7bcc"


def frozen_module(name, relative_path):
    source = subprocess.check_output(["git", "show", f"{BASELINE}:{relative_path}"], cwd=ROOT, text=True)
    module = types.ModuleType(name)
    module.__file__ = relative_path
    sys.modules[name] = module
    exec(compile(source, relative_path, "exec"), module.__dict__)
    return module


def measure(fn, repeats=3):
    values = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        values.append(time.perf_counter() - start)
    return statistics.median(values)


def main():
    from commontrace.ingest import Chunk, multimodal, pipeline, sanitize_contextualizer_text

    old_pipeline = frozen_module("phase6_baseline_pipeline", "commontrace/ingest/pipeline.py")
    old_ingest = frozen_module("phase6_baseline_ingest", "commontrace/ingest/__init__.py")
    old_mm = frozen_module("phase6_baseline_multimodal", "commontrace/ingest/multimodal.py")
    results = {
        "baseline": BASELINE,
        "python": sys.version,
        "platform": platform.platform(),
        "chunking": [],
        "malformed_parsers": [],
    }
    for mib in (1, 2, 4, 8):
        chunk = Chunk("a" * (mib * 1024 * 1024), "benchmark", "benchmark")

        def old():
            return sum(1 for _ in old_pipeline.TextChunker().apply([chunk]))

        def new():
            return sum(1 for _ in pipeline.TextChunker().apply([chunk]))

        assert old() == new()
        before, after = measure(old), measure(new)
        results["chunking"].append(
            {"mib": mib, "baseline_seconds": before, "current_seconds": after, "speedup": before / after}
        )
    chunk = Chunk("a" * (4 * 1024 * 1024), "benchmark", "benchmark")
    peaks = {}
    profiles = []
    for label, module in [("baseline", old_pipeline), ("current", pipeline)]:

        def run():
            return sum(1 for _ in module.TextChunker().apply([chunk]))

        tracemalloc.start()
        run()
        peaks[label] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        profiler = cProfile.Profile()
        profiler.runcall(run)
        output = io.StringIO()
        pstats.Stats(profiler, stream=output).sort_stats("cumulative").print_stats(12)
        profiles.append(label + "\n" + output.getvalue())
    results["chunker_auxiliary_peak_bytes_4mib"] = peaks
    with tempfile.TemporaryDirectory() as temp:
        for n in (2000, 4000, 8000):
            for label, suffix, content, function in [
                ("unclosed-pdf-BT", ".pdf", "%PDF-1.4\n" + "BT " * n + "(recoverable text) Tj", "extract_pdf_text"),
                ("escaped-pdf-opening-parens", ".pdf", "%PDF\nBT " + "\\(" * n + " Tj ET", "extract_pdf_text"),
                (
                    "unclosed-html-script",
                    ".html",
                    "<h1>Visible</h1><script>" * n + "private script text",
                    "extract_html_text",
                ),
            ]:
                path = pathlib.Path(temp) / ("document" + suffix)
                path.write_text(content)
                before = measure(lambda: getattr(old_mm, function)(str(path)))
                after = measure(lambda: getattr(multimodal, function)(str(path)))
                results["malformed_parsers"].append(
                    {
                        "case": label,
                        "tokens": n,
                        "baseline_seconds": before,
                        "current_seconds": after,
                        "speedup": before / after,
                    }
                )
    results["role_tag_cleanup"] = []
    for n in (1000, 2000, 4000, 8000):
        text = "<system>" * n + "document text"
        before = measure(lambda: old_ingest.sanitize_contextualizer_text(text))
        after = measure(lambda: sanitize_contextualizer_text(text))
        results["role_tag_cleanup"].append(
            {"tokens": n, "baseline_seconds": before, "current_seconds": after, "speedup": before / after}
        )
    (ROOT / "research" / "ingestion-security-phase6-results.json").write_text(json.dumps(results, indent=2) + "\n")
    (ROOT / "research" / "ingestion-security-phase6-profile.txt").write_text("\n".join(profiles))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
