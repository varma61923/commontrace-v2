from __future__ import annotations

import argparse
import os
import sys

from commontrace import paths
from commontrace.commands._shellout import run_script


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "bench",
        help="Run the memory health benchmark (lesson_quality, implicit_retrieval, transfer_gap), "
        "or --pilot for the five business-outcome metrics.",
    )
    p.add_argument("--n", type=int, default=0, help="Number of recent episodes (default: all)")
    p.add_argument("--html", action="store_true", help="Output HTML to memory/benchmark_reports/")
    p.add_argument("--json", action="store_true", help="Raw JSON output to stdout")
    p.add_argument(
        "--save", action="store_true",
        help="Deprecated no-op: every run persists its JSON report by default now.",
    )
    p.add_argument(
        "--no-save", action="store_true",
        help="Do not save benchmark results to memory/benchmark_reports/",
    )
    p.add_argument(
        "--diff", action="store_true",
        help="Compare against last saved benchmark",
    )
    p.add_argument(
        "--history", action="store_true",
        help="Show historical benchmark trend",
    )
    p.add_argument(
        "--strict", action="store_true",
        help="Fail if any regression detected",
    )
    p.add_argument("--threshold-quality", type=float, default=None, help="lesson_quality alert threshold")
    p.add_argument("--threshold-retrieval", type=float, default=None, help="implicit_retrieval strict alert threshold")
    p.add_argument("--threshold-never-hit", type=float, default=None, help="Never-hit lesson ratio alert threshold")
    p.add_argument(
        "--threshold-unimodal", type=float, default=None,
        help="Unimodal importance-distribution alert threshold",
    )
    p.add_argument("--threshold-semantic", type=float, default=None, help="Semantic similarity threshold")
    p.add_argument("--threshold-lexical", type=float, default=None,
                   help="Warn on near-duplicate lessons at or above this lexical similarity (0-1).")
    p.add_argument("--threshold-freshness", type=float, default=None,
                   help="Warn when the fraction of recently-hit lessons falls below this (0-1).")
    p.add_argument("--threshold-composite", type=float, default=None,
                   help="Warn when the combined health score falls below this (0-1).")
    p.add_argument("--dest", default=None, help="Override store root directory")
    p.add_argument(
        "--pilot", action="store_true",
        help="Compute the five pilot business-outcome metrics from Trace.outcome data "
        "(repeated-error, resolution, escalation, frustration rate, token/LLM-call cost), "
        "baseline vs. current. See protocol/PROTOCOL.md#11-pilot-outcome-metrics.",
    )
    p.add_argument("--agent-type", default=None, help="--pilot only: filter to one agent_type")
    p.add_argument(
        "--retrieval", action="store_true",
        help="Measure retrieval quality PER FIELD against the labelled cross-field "
             "corpus (commontrace/fixtures/fields/): precision@1, recall@k, MRR, and the "
             "pollution ratio that turns retrieval imprecision into a wrong causal "
             "verdict. This is what keeps 'works for any agent type' a tested property "
             "rather than a claim.",
    )
    p.add_argument(
        "--max-pollution", type=float, default=None,
        help="--retrieval only: fail if any field's pollution ratio exceeds this.",
    )
    p.add_argument(
        "--max-spread", type=float, default=None,
        help="--retrieval only: fail if the worst field's pollution exceeds the best "
             "field's by more than this multiple.",
    )
    # Conversation-benchmark wiring: --dataset routes bench to the judge-driven
    # conversation harness (benchmarks/conversation_bench.py). Without
    # --dataset, bench behaves (and prints) exactly as before.
    p.add_argument("--dataset", choices=("locomo", "longmemeval", "dolphin", "beam"), default=None,
                   help="conversation dataset to benchmark; activates the unified conversation harness")
    p.add_argument("--data", default=None, help="--dataset only: dataset file/directory path")
    p.add_argument("--judge", choices=("auto", "generic", "longmemeval", "locomo", "beam"), default="auto",
                   help="--dataset only: judge protocol (default: auto, matching the dataset)")
    p.add_argument("--budget", default=None, help="--dataset only: token budget(s), comma list")
    p.add_argument("--limit", type=int, default=0, help="--dataset only: question/case limit")
    p.add_argument("--seed", type=int, default=0, help="--dataset only: sampling seed")
    p.add_argument("--answer", action="store_true",
                   help="--dataset only: model-answer and judge with COMMONTRACE_LLM_*")
    p.add_argument("--answer-model", default=None, help="--dataset only: answer model")
    p.add_argument("--judge-model", default=None, help="--dataset only: judge model")
    p.add_argument("--modes", default=None, help="--dataset only: comma list of memory, full-context, no-memory")
    p.add_argument("--embedder", default=None,
                   help="--dataset only: none, a local model (arctic-m, minilm, bge-small, e5-small, nomic) or "
                        "<provider>:<model>[@dims] (openai, gemini, voyage, cohere, ollama, compat)")
    p.add_argument("--rerank", default=None, choices=("auto", "none", "cross-encoder", "cross-encoder-fast"),
                   help="--dataset only: reranker mode")
    p.add_argument("--neighbours", type=int, default=None, help="--dataset only: neighbour turns")
    p.add_argument("--rerank-blend", type=float, default=None, help="--dataset only: rerank blend")
    p.add_argument("--profile-facts", type=int, default=None, help="--dataset only: profile facts hint count")
    p.add_argument("--personas", default="", help="--dataset only: dolphin persona list")
    p.add_argument("--max-cost", type=float, default=None, help="--dataset only: USD cost ceiling")
    p.add_argument("--cache-dir", default=None, help="--dataset only: LLM disk cache directory")
    p.add_argument("--no-cache", action="store_true", help="--dataset only: disable LLM disk cache")
    p.add_argument("--bootstrap", action="store_true", help="--dataset only: bootstrap 95%% CIs")
    p.add_argument("--compare", default=None, help="--dataset only: baseline JSON for paired comparison")
    p.add_argument("--out", default=None, help="--dataset only: write full results (with rows) to this JSON file")
    p.add_argument("--chunk-set", default=None,
                   help="--dataset only: reuse/store parsed evaluation inputs under "
                        "<root>/runs/chunk_sets/<name>/ (manifest snapshot makes sweeps cheap)")
    p.set_defaults(func=run)


def _conversation_bench_script() -> str | None:
    """Locate benchmarks/conversation_bench.py in the source checkout (newest ancestor wins)."""
    override = os.environ.get("COMMONTRACE_CONVERSATION_BENCH")
    if override and os.path.isfile(override):
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    for up in range(8):
        candidate = os.path.normpath(os.path.join(here, *([".."] * (up + 2)), "benchmarks", "conversation_bench.py"))
        if os.path.isfile(candidate):
            return candidate
    return None


def conversation_bench_argv(args: argparse.Namespace) -> list[str]:
    """Build the conversation_bench.py command line from the bench CLI args."""
    script = _conversation_bench_script()
    if script is None:
        raise FileNotFoundError("benchmarks/conversation_bench.py not found; set COMMONTRACE_CONVERSATION_BENCH")
    argv = [sys.executable, script, "--dataset", args.dataset, "--data", args.data, "--judge", args.judge]
    if args.budget is not None:
        argv += ["--budget", str(args.budget)]
    if args.limit:
        argv += ["--limit", str(args.limit)]
    if args.seed:
        argv += ["--seed", str(args.seed)]
    if args.answer:
        argv.append("--answer")
    if args.answer_model:
        argv += ["--answer-model", args.answer_model]
    if args.judge_model:
        argv += ["--judge-model", args.judge_model]
    if args.modes:
        argv += ["--modes", args.modes]
    if args.embedder:
        argv += ["--embedder", args.embedder]
    if args.rerank:
        argv += ["--rerank", args.rerank]
    if args.neighbours is not None:
        argv += ["--neighbours", str(args.neighbours)]
    if args.rerank_blend is not None:
        argv += ["--rerank-blend", str(args.rerank_blend)]
    if args.profile_facts is not None:
        argv += ["--profile-facts", str(args.profile_facts)]
    if args.personas:
        argv += ["--personas", args.personas]
    if args.max_cost is not None:
        argv += ["--max-cost", str(args.max_cost)]
    if args.cache_dir:
        argv += ["--cache-dir", args.cache_dir]
    if args.no_cache:
        argv.append("--no-cache")
    if args.bootstrap:
        argv.append("--bootstrap")
    if args.compare:
        argv += ["--compare", args.compare]
    if args.out:
        argv += ["--out", args.out]
    if args.chunk_set:
        argv += ["--chunk-set", args.chunk_set]
    if args.dest:
        argv += ["--root", args.dest]
    return argv


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if args.dataset:
        if not args.data:
            print("[commontrace] --dataset requires --data <path to the dataset file or directory>", file=sys.stderr)
            return 2
        if args.retrieval or args.pilot:
            print("[commontrace] --dataset measures conversation memory; run --retrieval/--pilot separately.",
                  file=sys.stderr)
            return 2
        try:
            argv = conversation_bench_argv(args)
        except FileNotFoundError as exc:
            print(f"[commontrace] {exc}", file=sys.stderr)
            return 1
        import subprocess

        proc = subprocess.run(argv)  # nosec B603 - argv is built from flags, no shell
        return proc.returncode
    if args.retrieval:
        if args.pilot:
            print(
                "[commontrace] --retrieval and --pilot measure different things "
                "(retrieval quality per field vs. this fleet's business outcomes). "
                "Run them separately.",
                file=sys.stderr,
            )
            return 2
        extra = []
        if args.json:
            extra.append("--json")
        if args.max_pollution is not None:
            extra += ["--max-pollution", str(args.max_pollution)]
        if args.max_spread is not None:
            extra += ["--max-spread", str(args.max_spread)]
        return run_script(
            root,
            "benchmark/measure_retrieval.py",
            extra,
            "measure_retrieval.py ships inside the commontrace package, so this "
            "usually means a damaged install -- try `pip install --force-reinstall "
            "commontrace`. It needs nothing beyond PyYAML.",
        )
    if args.pilot:
        if args.strict:
            print(
                "[commontrace] --strict is not supported with --pilot: pilot_metrics.py "
                "reports baseline-vs-current numbers but implements no threshold-based "
                "pass/fail logic to enforce. Drop --strict, or use `bench` (without "
                "--pilot) for a --strict-gated CI run.",
                file=sys.stderr,
            )
            return 2
        extra = []
        if args.html:
            extra.append("--html")
        if args.json:
            extra.append("--json")
        if args.dest:
            extra += ["--dest", args.dest]
        if args.agent_type:
            extra += ["--agent-type", args.agent_type]
        return run_script(
            root,
            "benchmark/pilot_metrics.py",
            extra,
            "pilot_metrics.py ships inside the commontrace package, so this usually "
            "means a damaged install -- try `pip install --force-reinstall commontrace`. "
            "It needs nothing beyond PyYAML.",
        )
    extra = []
    if args.n:
        extra += [f"--n={args.n}"]
    if args.html:
        extra.append("--html")
    if args.json:
        extra.append("--json")
    if args.save:
        extra.append("--save")
    if args.no_save:
        extra.append("--no-save")
    if args.diff:
        extra.append("--diff")
    if args.history:
        extra.append("--history")
    if args.strict:
        extra.append("--strict")
    if args.threshold_quality is not None:
        extra += [f"--threshold-quality={args.threshold_quality}"]
    if args.threshold_retrieval is not None:
        extra += [f"--threshold-retrieval={args.threshold_retrieval}"]
    if args.threshold_never_hit is not None:
        extra += [f"--threshold-never-hit={args.threshold_never_hit}"]
    if args.threshold_unimodal is not None:
        extra += [f"--threshold-unimodal={args.threshold_unimodal}"]
    if args.threshold_semantic is not None:
        extra += [f"--threshold-semantic={args.threshold_semantic}"]
    for flag, value in (
        ("--threshold-lexical", args.threshold_lexical),
        ("--threshold-freshness", args.threshold_freshness),
        ("--threshold-composite", args.threshold_composite),
    ):
        if value is not None:
            extra += [f"{flag}={value}"]

    return run_script(
        root,
        "benchmark/measure_performance.py",
        extra,
        "measure_performance.py ships inside the commontrace package, so this usually "
        "means a damaged install -- try `pip install --force-reinstall commontrace`. "
        "It needs nothing beyond PyYAML.",
    )
