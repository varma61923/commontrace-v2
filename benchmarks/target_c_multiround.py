"""Run the existing coverage-driven second pass as a Target C diagnostic.

Use the same arguments as ``benchmarks.conversation_bench``. This opt-in
wrapper changes no retrieval code or default; it only sets ``Options.second_pass``
for the benchmark run.
"""
from __future__ import annotations

from benchmarks import conversation_bench
from unimem.conversation import Options

if not hasattr(Options(), "second_pass"):
    raise SystemExit("this UniMem revision does not support Options.second_pass")

_product_options = conversation_bench.product_options


def _multiround_options(**requested):
    requested["second_pass"] = True
    return _product_options(**requested)


conversation_bench.product_options = _multiround_options

if __name__ == "__main__":
    raise SystemExit(conversation_bench.main())
