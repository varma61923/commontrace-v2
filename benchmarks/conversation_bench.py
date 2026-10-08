"""LoCoMo and LongMemEval through CommonTrace's conversation memory, end to end.

Each conversation is written with `Store.add` exactly as an agent would write it,
and each question is answered from `recall`'s context. Without a model it measures
what a memory layer owns: how much of the gold evidence reaches the context, how
many tokens that context costs, and whether the gold answer appears in it. With
COMMONTRACE_LLM_* configured, `--answer` also has a model answer from the context
and a judge grade the answer, as published results do.

    python benchmarks/conversation_bench.py --dataset locomo --data locomo10.json
    python benchmarks/conversation_bench.py --dataset longmemeval --data longmemeval_s.json \
        --limit 60 --budget 2000
    python benchmarks/conversation_bench.py --dataset dolphin --data path/to/dolphinbench --budget 1500,4000

For DolphinBench the evidence of a test is the history messages that establish its
load-bearing facts; the query is the task request as the user typed it, asked on
the test's anchor date.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.artifacts import model_artifact
from benchmarks.cache import BenchmarkCache, CostGuard  # noqa: E402
from benchmarks.compare import clustered_estimates, ranking_metrics
from benchmarks.compare import compare_runs as paired_compare  # noqa: E402
from benchmarks.completeness import bucket_counts, grade_context_completeness  # noqa: E402
from benchmarks.judges import (  # noqa: E402
    BEAM_ABILITIES,
    get_default_judge_for_dataset,
    get_judge,
    is_scorable_category,
)
from benchmarks.measurement import (  # noqa: E402
    SCHEMA_VERSION,
    adapter_digest,
    canonical_digest,
    dataset_digest,
    gold_sessions,
    gold_turns,
    product_digest,
    question_digest,
    ranking_fields,
    shared_source_clusters,
)
from benchmarks.requests import benchmark_binding, bounded_complete, text_tokens  # noqa: E402
from benchmarks.vendor_adapters import PROFILES, vendor_profile  # noqa: E402
from commontrace.conversation import Options, Store, recall  # noqa: E402
from commontrace.conversation.search import tokens  # noqa: E402

LOCOMO_CATEGORIES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop"}


# --------------------------------------------------------------------------
# Chunk-set reuse: parsed evaluation cases cached under runs/chunk_sets/.
#
# Preparing cases means parsing the (possibly large) dataset files into
# (space, sessions, now, questions) tuples. Sweeps vary budget/embedder/mode/
# answer-model, not the inputs, so the parsed inputs are cached on disk:
#
#   <root>/runs/chunk_sets/<name>/
#       manifest.json   snapshot of the parameters that define this chunk set
#       chunks.jsonl    one normalized case per line (space/sessions/now/questions)
#
# <name> defaults to "<dataset>-<hash8>" where hash8 is a sha256 prefix of the
# fingerprint (dataset, data path, file size+mtime, limit, seed, personas);
# passing --chunk-set NAME overrides the directory name. A reused directory is
# only trusted when its manifest fingerprint matches the current parameters.
# --------------------------------------------------------------------------

CHUNK_SET_LAYOUT = "<root>/runs/chunk_sets/<name>/{manifest.json,chunks.jsonl}"


def chunk_fingerprint(dataset: str, data: str, limit: int, seed: int, personas: str) -> dict:
    path = os.path.abspath(data)
    try:
        st = os.stat(path)
        size, mtime = st.st_size, int(st.st_mtime)
    except OSError:
        size, mtime = -1, -1
    return {"dataset": dataset, "data": path, "size": size, "mtime": mtime,
            "limit": int(limit or 0), "seed": int(seed or 0), "personas": personas or "",
            "dataset_sha256": dataset_digest(path), "adapter_sha256": adapter_digest(__file__)}


def _payload_from_cases(cases) -> list[dict]:
    out = []
    for space, sessions, now, questions in cases:
        normalized_questions = []
        for q in questions:
            qn = dict(q)
            for key in ("evidence", "sessions"):
                if isinstance(qn.get(key), set):
                    qn[key] = sorted(qn[key])
            normalized_questions.append(qn)
        out.append({"space": space, "now": now,
                    "sessions": [[name, date, messages] for name, date, messages in sessions],
                    "questions": normalized_questions})
    return out


def _cases_from_payload(payload) -> list:
    out = []
    for case in payload:
        sessions = [(name, date, messages) for name, date, messages in case["sessions"]]
        questions = []
        for q in case["questions"]:
            qn = dict(q)
            for key in ("evidence", "sessions"):
                qn[key] = set(qn.get(key) or [])
            questions.append(qn)
        out.append((case["space"], sessions, case.get("now"), questions))
    return out


def _load_cases(args) -> list:
    if args.dataset == "dolphin":
        return list(dolphin_cases(args.data, args.personas, args.limit))
    if args.dataset == "beam":
        return list(beam_cases(args.data, args.limit))
    if args.dataset == "locomo":
        return sample_cases(list(locomo_cases(args.data)), args.limit, args.seed)
    return list(longmemeval_cases(args.data, args.limit, args.seed))


def prepare_chunk_set(args) -> tuple[list, dict]:
    """Load or build the chunked evaluation inputs for this run.

    Returns (cases, info): cases as (space, sessions, now, questions) tuples,
    and info describing where the chunk set lives and whether it was reused.
    """
    import datetime as dt
    import hashlib as _hashlib

    fingerprint = chunk_fingerprint(args.dataset, args.data, args.limit, args.seed,
                                    getattr(args, "personas", ""))
    digest = _hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode("utf-8")).hexdigest()
    name = getattr(args, "chunk_set", None) or f"{args.dataset}-{digest[:8]}"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if name in (".", ".."):
        raise ValueError("chunk-set name must not be a relative directory")
    fdir = os.path.join(args.root, "runs", "chunk_sets", name)
    manifest_path = os.path.join(fdir, "manifest.json")
    chunks_path = os.path.join(fdir, "chunks.jsonl")

    if os.path.isfile(manifest_path) and os.path.isfile(chunks_path):
        try:
            manifest = json.load(open(manifest_path, encoding="utf-8"))
        except (ValueError, OSError):
            manifest = None
        if (manifest and manifest.get("fingerprint") == fingerprint
                and manifest.get("chunks_sha256") == dataset_digest(chunks_path)):
            with open(chunks_path, encoding="utf-8") as fh:
                payload = [json.loads(line) for line in fh if line.strip()]
            info = dict(manifest.get("info") or {})
            info.update({"name": name, "path": fdir, "reused": True, "layout": CHUNK_SET_LAYOUT,
                         "fingerprint": fingerprint})
            return _cases_from_payload(payload), info

    cases = _load_cases(args)
    payload = _payload_from_cases(cases)
    cases = _cases_from_payload(payload)
    os.makedirs(fdir, exist_ok=True)
    with open(chunks_path, "w", encoding="utf-8") as fh:
        for case in payload:
            fh.write(json.dumps(case, ensure_ascii=False, default=str) + "\n")
    info = {"name": name, "path": fdir, "reused": False, "layout": CHUNK_SET_LAYOUT,
            "dataset": args.dataset, "cases": len(cases),
            "questions": sum(len(qs) for _s, _sess, _n, qs in cases), "fingerprint": fingerprint}
    manifest = {"fingerprint": fingerprint, "info": info, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "layout": CHUNK_SET_LAYOUT, "chunks_sha256": dataset_digest(chunks_path)}
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    return cases, info


def _evidence_texts(sessions, evidence_ids) -> dict[str, str]:
    """Map each gold evidence id to the text that establishes it.

    Exact turn-ref matches win; a trailing '#k' message fragment is accepted as
    the same evidence session (DolphinBench-style session ids).
    """
    by_ref: dict[str, list[str]] = {}
    for _s, _d, messages in sessions:
        for m in messages:
            by_ref.setdefault(str(m["id"]), []).append(m.get("text") or "")
    out = {}
    for eid in evidence_ids:
        texts = by_ref.get(str(eid))
        if texts is None:
            texts = [t for rid, ts in by_ref.items() if rid.startswith(str(eid) + "#") for t in ts]
        if texts:
            out[str(eid)] = "\n".join(texts)
    return out


def sample_cases(cases, limit, seed):
    """Exactly limit questions, round-robin across type and conversation strata."""
    strata = defaultdict(list)
    for index, (_space, _sessions, _now, questions) in enumerate(cases):
        for question in questions:
            strata[(question["type"], index)].append(question)
    if not limit or limit >= sum(map(len, strata.values())):
        return cases
    rng = random.Random(seed)
    for key in sorted(strata):
        rng.shuffle(strata[key])
    categories = sorted({kind for kind, _index in strata})
    indices = sorted({index for _kind, index in strata})
    # Rotate category across conversations before revisiting either dimension.
    schedule = [(categories[(i + phase) % len(categories)], index)
                for phase in range(len(categories)) for i, index in enumerate(indices)]
    selected = defaultdict(list)
    count = 0
    while count < limit:
        for key in schedule:
            if strata[key] and count < limit:
                selected[key[1]].append(strata[key].pop())
                count += 1
    return [(space, sessions, now, selected[i])
            for i, (space, sessions, now, _qs) in enumerate(cases) if selected[i]]


def locomo_cases(path: str):
    for conv in json.load(open(path)):
        c = conv["conversation"]
        sessions = []
        n = 1
        while f"session_{n}" in c:
            messages = []
            for turn in c[f"session_{n}"]:
                text = turn["text"]
                if turn.get("blip_caption"):
                    text += f" [shares a photo: {turn['blip_caption']}]"
                messages.append({"id": turn["dia_id"], "speaker": turn["speaker"], "text": text})
            sessions.append((f"session {n}", c.get(f"session_{n}_date_time"), messages))
            n += 1
        questions = []
        for i, qa in enumerate(conv["qa"]):
            if qa.get("category") not in LOCOMO_CATEGORIES:
                continue
            gold = {str(e).strip() for e in qa.get("evidence", []) if str(e).strip()}
            questions.append({"id": f"{conv['sample_id']}-{i}", "question": qa["question"],
                              "answer": str(qa.get("answer", "")), "type": LOCOMO_CATEGORIES[qa["category"]],
                              "evidence": gold, "sessions": set()})
        yield conv["sample_id"], sessions, None, questions


def longmemeval_cases(path: str, limit: int, seed: int):
    data = json.load(open(path))
    if limit and limit < len(data):
        by_type = defaultdict(list)
        for q in data:
            by_type[q["question_type"]].append(q)
        rng = random.Random(seed)
        # Balanced categories with remainder redistribution; exactly limit items.
        categories = sorted(by_type)
        for category in categories:
            rng.shuffle(by_type[category])
        selected = []
        while len(selected) < limit:
            for category in categories:
                if by_type[category] and len(selected) < limit:
                    selected.append(by_type[category].pop())
        data = selected
    for q in data:
        sessions, evidence = [], set()
        for sid, date, session in zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]):
            messages = []
            for j, turn in enumerate(session):
                ref = f"{sid}#{j}"
                if turn.get("has_answer"):
                    evidence.add(ref)
                messages.append({"id": ref, "role": turn["role"], "speaker": turn["role"],
                                 "text": turn["content"]})
            sessions.append((sid, date, messages))
        kind = q["question_type"] + ("-abstain" if q["question_id"].endswith("_abs") else "")
        question = {"id": q["question_id"], "question": q["question"], "answer": str(q["answer"]),
                    "type": kind, "evidence": evidence, "sessions": set(q["answer_session_ids"])}
        # LongMemEval treats every haystack session as history, but 43 of its 500
        # questions carry a clock time earlier than an evidence session on the same
        # day. Asking at the end of the question's day keeps recall's point-in-time
        # cutoff (nothing said after `now`) from hiding that history.
        yield q["question_id"], sessions, q["question_date"].split(" ")[0] + " 23:59", [question]


def dolphin_cases(path: str, personas: str = "", limit: int = 0):
    import glob

    import yaml

    names = [p for p in (personas.split(",") if personas else ("alex", "morgan", "riley")) if p]
    for persona in names:
        base = os.path.join(path, "registry", "personas", persona)
        history = yaml.safe_load(open(os.path.join(base, "life_sim.yaml"), encoding="utf-8"))["sessions"]
        facts = {f["id"]: f for f in yaml.safe_load(open(os.path.join(base, "facts.yaml"), encoding="utf-8"))["facts"]}
        by_day: dict[str, list] = defaultdict(list)
        for s in history:
            day = str(s["narrative_date"])[:10]
            for j, text in enumerate(s["messages"]):
                ref = s["id"] if len(s["messages"]) == 1 else f"{s['id']}#{j}"
                by_day[day].append({"id": ref, "speaker": "user", "role": "user", "text": text,
                                    "at": str(s["narrative_date"])})
        sessions = [(day, msgs[0]["at"], msgs) for day, msgs in sorted(by_day.items())]
        files = sorted(glob.glob(os.path.join(path, "tests", persona, "[0-9]*.yaml")))
        questions, now = [], None
        for f in files[:limit or None]:
            t = yaml.safe_load(open(f, encoding="utf-8"))
            now = str(t["narrative_anchor_date"])
            evidence = {str(sid) for fid in t["load_bearing_facts"] for sid in facts[fid]["source_session_ids"]}
            n = len(t["load_bearing_facts"])
            questions.append({"id": f"{persona}-{t['id']}", "question": t["test"], "answer": "",
                              "type": "1 fact" if n == 1 else ("2-3 facts" if n <= 3 else "4+ facts"),
                              "evidence": evidence, "sessions": set()})
        yield persona, sessions, now, questions


BEAM_MARK = re.compile(r"\s*->->\s*[\d, ]+\s*$")


def _ids(value) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, dict):
        return set().union(*(_ids(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple)) or hasattr(value, "tolist"):
        return {str(int(v)) for v in (value.tolist() if hasattr(value, "tolist") else value)
                if str(v).strip().lstrip("-").isdigit()}
    return {str(int(value))} if str(value).strip().isdigit() else set()


def beam_cases(path: str, limit: int = 0):
    """BEAM (Beyond a Million Tokens): each conversation's probing questions, ten abilities.
    Evidence is the chat messages each question cites; abstention questions cite none."""
    import ast
    import datetime as dt

    import pandas as pd

    frame = pd.read_parquet(path)
    for _, row in frame.iterrows():
        sessions, last = [], None
        for n, session in enumerate(row["chat"]):
            anchor = next((m["time_anchor"] for m in session if m.get("time_anchor") not in (None, "None")), None)
            when = None
            if anchor:
                try:
                    when = dt.datetime.strptime(str(anchor), "%B-%d-%Y").date().isoformat()
                except ValueError:
                    when = None
            last = when or last
            messages = [{"id": str(m["id"]), "role": m["role"], "speaker": m["role"],
                         "text": BEAM_MARK.sub("", str(m["content"] or ""))} for m in session]
            sessions.append((f"session {n + 1}", when or last, messages))
        probing = ast.literal_eval(row["probing_questions"]) if isinstance(row["probing_questions"], str) \
            else row["probing_questions"]
        questions = []
        for category, items in probing.items():
            for i, q in enumerate(items):
                answer = q.get("answer") or q.get("ideal_answer") or q.get("ideal_response") or \
                    q.get("ideal_summary") or ""
                questions.append({
                    "id": f"beam{row['conversation_id']}-{category}-{i}",
                    "question": q["question"],
                    "answer": str(answer),
                    "type": category,
                    "evidence": _ids(q.get("source_chat_ids")),
                    "sessions": set(),
                    "rubric": q.get("rubric") or [],
                    "raw": q,
                })
        yield f"beam{row['conversation_id']}", sessions, None, questions
        limit -= 1
        if limit == 0:
            break


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def answer_in(context: str, answer: str) -> bool | None:
    answer = _norm(answer)
    if not answer or len(answer.split()) > 6:
        return None
    return f" {answer} " in f" {_norm(context)} "


from commontrace.conversation.answer import ANSWER as ANSWER_PROMPT  # noqa: E402  the product's own prompt

JUDGE_PROMPT = """Grade an answer against a gold answer. Be generous: the answer is CORRECT if it
contains the same information as the gold answer, even if phrased differently or longer.
For time questions, the same date or period in another format is CORRECT.

Question: {question}
Gold answer: {gold}
Answer: {answer}

Reply with exactly one word: CORRECT or WRONG."""


def make_full_context(sessions: list, budget: int | None) -> tuple[str, int]:
    """Assemble chronological raw history; None retains the entire history."""
    blocks = []
    spent = 0
    for session, date, messages in sessions:
        header = f"=== {session} ({date}) ===" if date else f"=== {session} ==="
        header_tok = tokens(header) + 1
        lines = [header]
        spent += header_tok
        if budget is not None and spent > budget:
            break
        for m in messages:
            speaker = m.get("speaker") or m.get("role") or "speaker"
            text = m.get("text") or ""
            line = f"{speaker}: {text}"
            cost = tokens(line) + 1
            if budget is not None and spent + cost > budget:
                break
            lines.append(line)
            spent += cost
        blocks.append("\n".join(lines))
        if budget is not None and spent >= budget:
            break
    ctx = "\n\n".join(blocks)
    return ctx, tokens(ctx)


def _p50_p95(values) -> dict[str, float] | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    p50 = vals[int(0.50 * (n - 1))]
    p95 = vals[int(0.95 * (n - 1))]
    return {"p50_ms": round(p50 * 1000, 1), "p95_ms": round(p95 * 1000, 1)}


def _get_llm_config(model: str):
    from commontrace import llm

    try:
        base = llm.load_config()
        return llm.Config(
            provider=base.provider,
            model=model,
            api_key=base.api_key,
            base_url=base.base_url,
            region=base.region,
            project=base.project,
            cache_namespace=base.cache_namespace,
        )
    except Exception:
        provider = os.environ.get("COMMONTRACE_LLM_PROVIDER", "openai-compatible")
        api_key = os.environ.get("COMMONTRACE_LLM_API_KEY", "")
        base_url = os.environ.get("COMMONTRACE_LLM_BASE_URL", "")
        return llm.Config(provider=provider, model=model, api_key=api_key, base_url=base_url or None,
                          region=os.environ.get("COMMONTRACE_LLM_REGION") or os.environ.get("AWS_REGION") or None,
                          project=os.environ.get("COMMONTRACE_LLM_PROJECT") or None,
                          cache_namespace=os.environ.get("COMMONTRACE_LLM_CACHE_NAMESPACE") or None)


def grade_answer(
    question: dict,
    context: str,
    now: str | None,
    ans_model: str,
    j_model: str,
    judge_inst: Any,
    cache: BenchmarkCache | None,
    cost_guard: CostGuard | None,
    explain: dict | None = None,
    output_limit: int = 1536,
    tokenizer: str | None = None,
) -> dict:
    """Generate answer from context with ans_model and grade with judge_inst using j_model."""
    guard = cost_guard or CostGuard()
    calls = []

    def call_cached(prompt: str, model: str, stage: str) -> tuple[str, dict, float, float]:
        t0 = time.perf_counter()
        cfg = _get_llm_config(model)
        binding = benchmark_binding(cfg, output_limit=output_limit) if cache is not None else None
        if cache is not None:
            hit = cache.get(model, prompt, binding=binding)
            if hit is not None:
                resp, usage, cost = hit
                guard.record_call(cost, is_cached=True)
                calls.append({"stage": stage, "model": model, "cache_hit": True,
                              "usage": usage, "cost_usd": 0.0, "historical_cost_usd": cost})
                return resp, usage, 0.0, 0.0

        resp, usage, cost = bounded_complete(prompt, cfg, guard, output_limit=output_limit)
        latency = time.perf_counter() - t0
        calls.append({"stage": stage, "model": model, "cache_hit": False, "usage": usage,
                      "cost_usd": cost, "historical_cost_usd": 0.0})
        if cache is not None:
            cache.put(model, prompt, resp, usage, cost, binding=binding)
        return resp, usage, latency, cost

    # Step 1: Generate answer
    ctx_to_use = context
    if explain and explain.get("abstain") and ctx_to_use:
        ctx_to_use = ctx_to_use + "\n\n[Note: No mentions of this subject were found in memory. If asking for a specific detail that was never recorded, state that you do not have this information.]"

    ans_prompt = ANSWER_PROMPT.format(
        context=ctx_to_use,
        question=question["question"],
        now=now or "now",
    )
    ans_text, ans_usage, ans_lat, ans_cost = call_cached(ans_prompt, ans_model, "answer")

    # Step 2: Judge answer
    judge_latencies = []
    judge_costs = []

    def judge_complete(prompt: str) -> tuple[str, dict]:
        resp, usage, lat, c = call_cached(prompt, j_model, "judge")
        judge_latencies.append(lat)
        judge_costs.append(c)
        return resp, usage

    grade_res = judge_inst.grade(
        question,
        ans_text,
        complete_fn=judge_complete,
        model=j_model,
    )

    ans_in_tok = ans_usage.get("input_tokens") or ans_usage.get("prompt_tokens") or 0
    ans_out_tok = ans_usage.get("output_tokens") or ans_usage.get("completion_tokens") or 0
    j_lat_total = sum(judge_latencies)
    j_cost_total = sum(judge_costs)

    res = {
        "answer": ans_text.strip(),
        "correct": grade_res.get("correct"),
        "score": grade_res.get("score"),
        "answer_tokens_in": ans_in_tok,
        "answer_tokens_out": ans_out_tok,
        "context_tokens": text_tokens(context, tokenizer) if tokenizer else tokens(context),
        "context_token_accounting": tokenizer or "ceil-characters-divided-by-four",
        "answer_latency_s": ans_lat,
        "judge_latency_s": j_lat_total,
        "answer_cost_usd": ans_cost,
        "judge_cost_usd": j_cost_total,
        "judge": judge_inst.name,
        "judge_profile": getattr(judge_inst, "profile", judge_inst.name),
        "judge_model": j_model,
        "answer_model": ans_model,
        "completion_calls": calls,
        "answer_cache_hit": calls[0]["cache_hit"],
        "judge_all_cached": all(c["cache_hit"] for c in calls if c["stage"] == "judge"),
        "historical_cached_cost_usd": sum(c["historical_cost_usd"] for c in calls),
    }
    if "ability" in grade_res:
        res["beam_ability"] = grade_res["ability"]
    if "details" in grade_res:
        res["beam_details"] = grade_res["details"]
    return res


def evaluation_config_binding(config: object) -> dict[str, object]:
    # Endpoints may embed userinfo or query credentials; bind their exact
    # configuration without publishing URL bytes or private account metadata.
    fields = {k: getattr(config, k) for k in ("provider", "model", "base_url", "region", "project")}
    return {"provider": fields["provider"], "model": fields["model"],
            "configuration_sha256": question_digest(fields)}


def product_digest_for_judges(repository: str) -> str:
    import hashlib

    directory = os.path.join(repository, "benchmarks", "judges")
    digest = hashlib.sha256()
    for name in sorted(os.listdir(directory)):
        if name.endswith(".py"):
            digest.update(name.encode("utf-8") + b"\0")
            with open(os.path.join(directory, name), "rb") as source:
                digest.update(source.read())
    return digest.hexdigest()


def run(args) -> dict:
    repository = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    initial_product = product_digest(repository)
    if args.limit < 0 or args.seed < 0:
        raise ValueError("limit and seed must be nonnegative")
    budgets = [int(b) for b in str(args.budget).split(",")]
    if not budgets or any(b <= 0 for b in budgets) or len(set(budgets)) != len(budgets):
        raise ValueError("budgets must be distinct positive integers")
    modes = [m.strip() for m in getattr(args, "modes", "memory").split(",") if m.strip()]
    if not modes:
        modes = ["memory"]
    if len(set(modes)) != len(modes) or any(m not in ("memory", "full-context", "budgeted-history", "no-memory")
                                          for m in modes):
        raise ValueError("modes must be distinct supported reference modes")
    os.makedirs(args.root, exist_ok=True)

    judge_choice = getattr(args, "judge", "auto")
    if judge_choice == "auto":
        j_name, def_j_model = get_default_judge_for_dataset(args.dataset)
    else:
        j_name = judge_choice
        _, def_j_model = get_default_judge_for_dataset(j_name)
    j_model = getattr(args, "judge_model", None) or def_j_model
    ans_model = getattr(args, "answer_model", None) or os.environ.get("COMMONTRACE_LLM_MODEL") or "claude-sonnet-5"

    judge_inst = None
    cache = None
    cost_guard = None
    if args.answer:
        judge_inst = get_judge(j_name, model=j_model)
        if not getattr(args, "no_cache", False):
            cdir = getattr(args, "cache_dir", None) or os.path.join(args.root, "benchmark_cache")
            cache = BenchmarkCache(cdir)
        cost_guard = CostGuard(max_cost_usd=getattr(args, "max_cost", None))

        # Each actual prompt (including full history and every rubric call)
        # reserves its upper bound immediately before dispatch.

    memory_adapter = getattr(args, "memory_adapter", "commontrace")
    if memory_adapter not in PROFILES:
        raise ValueError("unsupported memory adapter")
    opts = Options(
        budget=budgets[0],
        embedder=None if args.embedder == "none" else args.embedder,
        rerank=None if args.rerank == "none" else args.rerank,
        neighbours_before=args.neighbours,
        neighbours_after=args.neighbours,
        profile_facts=args.profile_facts,
        rerank_blend=args.rerank_blend,
    )

    # Cached prep: parse once, then every sweep with the same fingerprint reuses it.
    cases, chunk_info = prepare_chunk_set(args)
    spaces = [re.sub(r"[^A-Za-z0-9._-]", "_", case[0]) for case in cases]
    ids = [q["id"] for _space, _sessions, _now, qs in cases for q in qs]
    if len(set(spaces)) != len(spaces) or len(set(ids)) != len(ids) or not ids:
        raise ValueError("benchmark cases and questions must have unique, nonempty identities")
    clusters = shared_source_clusters(cases) if args.dataset == "longmemeval" else {c[0]: c[0] for c in cases}
    fingerprint = chunk_info["fingerprint"]
    provenance = {
        "dataset_sha256": fingerprint["dataset_sha256"],
        "adapter_sha256": fingerprint["adapter_sha256"],
        "product_sha256": initial_product,
        "harness_sha256": dataset_digest(__file__),
        "vendor_adapters_sha256": dataset_digest(os.path.join(repository, "benchmarks", "vendor_adapters.py")),
        "artifacts_source_sha256": dataset_digest(os.path.join(repository, "benchmarks", "artifacts.py")),
        "sampling": {"seed": args.seed, "limit": args.limit, "personas": getattr(args, "personas", "")},
        "evaluation": {"answer_enabled": args.answer, "token_accounting": "ceil-characters-divided-by-four",
                       "completeness_sha256": dataset_digest(os.path.join(repository, "benchmarks", "completeness.py"))},
    }

    if args.answer:
        cfg_a, cfg_j = _get_llm_config(ans_model), _get_llm_config(j_model)
        provenance["evaluation"].update({
            "reader_prompt_sha256": question_digest({"prompt": ANSWER_PROMPT}),
            "judge_source_sha256": product_digest_for_judges(repository),
            "completion_source_sha256": dataset_digest(os.path.join(repository, "commontrace", "llm.py")),
            "reader": evaluation_config_binding(cfg_a),
            "judge": evaluation_config_binding(cfg_j),
            "generation": {"max_tokens": getattr(args, "max_output_tokens", 1536), "temperature": 0,
                           "retries": 0, "tokenizer": getattr(args, "tokenizer", None)},
            "bounded_requests_sha256": dataset_digest(os.path.join(repository, "benchmarks", "requests.py")),
        })

    profile_config = None
    model_bindings = {}
    if memory_adapter == "commontrace":
        from commontrace import rerank_arm
        from commontrace.conversation import embed
        for name in ([embed.MODELS[args.embedder][0]] if args.embedder in embed.MODELS else []) + (
                [rerank_arm.MODELS[args.rerank][0]] if args.rerank in rerank_arm.MODELS else []):
            model_bindings[name] = model_artifact(name)
    provenance["evaluation"]["context_text_tokenizer"] = getattr(args, "tokenizer", None)
    rows_by_mode = {m: {b: [] for b in budgets} for m in modes}
    ingest_s, full_tokens = 0.0, []
    recall_seconds = {b: 0.0 for b in budgets}

    for space, sessions, now, questions in cases:
        with Store(args.root, re.sub(r"[^A-Za-z0-9._-]", "_", space)) as store:
            # A same-sized edited corpus must not reuse a stale canonical store.
            source_hash = question_digest({"sessions": sessions})
            previous_hash = store.get_meta("benchmark_sources_sha256")
            if previous_hash is not None and previous_hash != source_hash:
                raise ValueError("benchmark store contains a different source corpus; use a fresh --root")
            if previous_hash is None and store.stats()["turns"]:
                raise ValueError("benchmark store has unverified sources; use a fresh --root")
            t = time.perf_counter()
            if previous_hash is None:
                for session, date, messages in sessions:
                    store.add(session, messages, session_at=date)
                store.set_meta("benchmark_sources_sha256", source_hash)
                store.set_meta("benchmark_canonical_sha256", canonical_digest(store))
            if store.get_meta("benchmark_canonical_sha256") != canonical_digest(store):
                raise ValueError("benchmark source store was modified; use a fresh --root")
            initial_canonical = store.get_meta("benchmark_canonical_sha256")
            ingest_s += time.perf_counter() - t
            full_tokens.append(sum(tokens(m["text"]) for _s, _d, ms in sessions for m in ms))

            t = time.perf_counter()
            with vendor_profile(memory_adapter, store, now) as adapter:
                if adapter is not None:
                    ingest_s += time.perf_counter() - t
                    descriptor = adapter.descriptor
                    if profile_config is not None and profile_config != descriptor:
                        raise RuntimeError("vendor configuration changed between cases")
                    profile_config = descriptor
                for q in questions:
                    # Each budget follows the entire public recall path, including its
                    # temporal, eligibility, standing-instruction and graph fences.
                    turn_gold = gold_turns(q["evidence"], sessions)
                    session_gold = gold_sessions(turn_gold, q["sessions"], sessions)
                    mem_contexts = {}
                    for budget in budgets:
                        opts_b = Options(**{**opts.__dict__, "budget": budget})
                        t = time.perf_counter()
                        r = (adapter.retrieve(q["question"], budget) if adapter is not None
                                 else recall(store, q["question"], now=now, options=opts_b))
                        elapsed = time.perf_counter() - t
                        recall_seconds[budget] += elapsed
                        kept = store.turns(r.turns).values()
                        refs, sess = {tt.ref for tt in kept}, {tt.session for tt in kept}
                        source_ranking = ranking_fields(r.ranked, store.turns(r.ranked), turn_gold, session_gold)
                        ranking_scores = {
                            level + "_" + metric: value
                            for level in ("turn", "session")
                            for metric, value in ranking_metrics(source_ranking["ranked_" + level + "_ids"],
                                                                source_ranking["gold_" + level + "_ids"]).items()
                        }
                        mem_contexts[budget] = (r, refs, sess, elapsed, source_ranking, ranking_scores)

                    for mode in modes:
                        for budget in budgets:
                            r, refs, sess, recall_elapsed, source_ranking, ranking_scores = mem_contexts[budget]
                            mem_context, n_tokens = r.context, r.tokens
                            if mode == "memory":
                                ctx = mem_context
                                toks = n_tokens
                                ev = (len(turn_gold & refs) / len(turn_gold)) if turn_gold else None
                                comp = turn_gold <= refs if turn_gold else None
                                ses = (len(set(session_gold) & sess) / len(session_gold)) if session_gold else None
                                ans_in_ctx = answer_in(ctx, q["answer"])
                                conf = r.explain.get("confidence")
                                rtop = r.explain.get("rerank_top")
                            elif mode in ("full-context", "budgeted-history"):
                                ctx, toks = make_full_context(sessions, None if mode == "full-context" else budget)
                                ev = None
                                comp = None
                                ses = None
                                ans_in_ctx = answer_in(ctx, q["answer"])
                                conf = None
                                rtop = None
                            elif mode == "no-memory":
                                ctx = ""
                                toks = 0
                                ev = None
                                comp = None
                                ses = None
                                ans_in_ctx = False
                                conf = None
                                rtop = None
                            else:
                                raise ValueError(f"Unknown mode: {mode}")

                            # Lexical completeness grader: a second, model-free read on the
                            # same retrieved context, alongside the evidence-id metric above.
                            comp_lex = grade_context_completeness(
                                ctx, _evidence_texts(sessions, q["evidence"]), answer=q["answer"])

                            row = {
                                "id": q["id"],
                                "type": q["type"],
                                "cluster_id": clusters[space],
                                "question_sha256": question_digest(q),
                                "unresolved_gold_turn_ids": sorted(turn_gold - {
                                    str(message["id"]) for _s, _d, messages in sessions for message in messages}),
                                **source_ranking,
                                "recall_latency_s": recall_elapsed if mode == "memory" else None,
                                "effective_embedders": ([adapter.descriptor["embedder"]] if adapter is not None
                                                           and "embedder" in adapter.descriptor else sorted(store._embedders)),
                                "memory_adapter": memory_adapter,
                                "effective_rerank": (r.explain.get("rerank")
                                                     if "rerank_top" in r.explain else None),
                                # Reference-mode ranking metrics are intentionally undefined.
                                **(ranking_scores if mode == "memory" else {}),
                                "tokens": toks,
                                "context_text_tokens": (text_tokens(ctx, args.tokenizer)
                                                        if getattr(args, "tokenizer", None) else None),
                                "evidence": ev,
                                "complete": comp,
                                "session": ses,
                                "answer_in_context": ans_in_ctx,
                                "confidence": conf,
                                "rerank_top": rtop,
                                "mode": mode,
                                "completeness_bucket": comp_lex["bucket"],
                                "completeness_score": comp_lex["score"],
                                "completeness_present": comp_lex["present"],
                                "completeness_missing": comp_lex["missing"],
                            }
                            if "rubric" in q:
                                row["rubric"] = q["rubric"]
                            if args.answer:
                                grade_info = grade_answer(
                                    question=q,
                                    context=ctx,
                                    now=now,
                                    ans_model=ans_model,
                                    j_model=j_model,
                                    judge_inst=judge_inst,
                                    cache=cache,
                                    cost_guard=cost_guard,
                                    explain=r.explain if mode == "memory" else None,
                                    output_limit=getattr(args, "max_output_tokens", 1536),
                                    tokenizer=getattr(args, "tokenizer", None),
                                )
                                row.update(grade_info)
                            rows_by_mode[mode][budget].append(row)

            if canonical_digest(store) != initial_canonical:
                raise RuntimeError("canonical sources changed during measurement; discard this run")


    n = max(1, len(rows_by_mode[modes[0]][budgets[0]]))
    judge_info = {
        "judge": j_name,
        "judge_profile": getattr(judge_inst, "profile", j_name),
        "judge_model": j_model,
        "answer_model": ans_model,
    } if args.answer else {}

    results = {}
    for b in budgets:
        if len(modes) == 1:
            m = modes[0]
            results[b] = summarize(
                rows_by_mode[m][b], args, b, ingest_s, recall_seconds[b] / n, full_tokens, mode=m, judge_info=judge_info
            )
            results[b]["chunk_set"] = chunk_info
            results[b].update({"comparison_schema": SCHEMA_VERSION, "provenance": provenance})
        else:
            mode_summaries = {
                m: summarize(
                    rows_by_mode[m][b], args, b, ingest_s, recall_seconds[b] / n, full_tokens, mode=m, judge_info=judge_info
                )
                for m in modes
            }
            for summary in mode_summaries.values():
                summary.update({"comparison_schema": SCHEMA_VERSION, "provenance": provenance})
            acc_mem = mode_summaries.get("memory", {}).get("overall", {}).get("accuracy")
            acc_nm = mode_summaries.get("no-memory", {}).get("overall", {}).get("accuracy")
            acc_fc = mode_summaries.get("full-context", {}).get("overall", {}).get("accuracy")
            lift = {
                "vs_no_memory": round(acc_mem - acc_nm, 4) if (acc_mem is not None and acc_nm is not None) else None,
                "vs_full_context": round(acc_mem - acc_fc, 4) if (acc_mem is not None and acc_fc is not None) else None,
            }
            primary_mode = "memory" if "memory" in modes else modes[0]
            primary = mode_summaries[primary_mode]
            results[b] = {
                **primary,
                "modes": {m: s["overall"] for m, s in mode_summaries.items()},
                "modes_detail": mode_summaries,
                "memory_lift": lift,
                "chunk_set": chunk_info,
            }
    for summary in results.values():
        summary["retrieval_profile"] = profile_config or {"profile": "commontrace", "model_artifacts": model_bindings}
    if getattr(args, "bootstrap", False):
        for summary in results.values():
            targets = summary.get("modes_detail", {summary["mode"]: summary})
            for target in targets.values():
                target["bootstrap_95ci"] = clustered_estimates(target, seed=args.seed)
            if "modes_detail" in summary:
                summary["bootstrap_95ci"] = targets[summary["mode"]]["bootstrap_95ci"]
    if product_digest(repository) != initial_product or adapter_digest(__file__) != provenance["adapter_sha256"]:
        raise RuntimeError("benchmark source changed during measurement; discard this run")
    if any(model_artifact(name) != binding for name, binding in model_bindings.items()):
        raise RuntimeError("retrieval model artifacts changed during measurement")
    if dataset_digest(__file__) != provenance["harness_sha256"]:
        raise RuntimeError("benchmark harness changed during measurement")
    if dataset_digest(os.path.join(repository, "benchmarks", "vendor_adapters.py")) != provenance["vendor_adapters_sha256"]:
        raise RuntimeError("vendor adapter changed during measurement")
    if dataset_digest(os.path.join(repository, "benchmarks", "artifacts.py")) != provenance["artifacts_source_sha256"]:
        raise RuntimeError("artifact binding code changed during measurement")
    if args.answer:
        if dataset_digest(os.path.join(repository, "benchmarks", "requests.py")) != provenance["evaluation"]["bounded_requests_sha256"]:
            raise RuntimeError("bounded completion source changed during measurement")
        for summary in results.values():
            summary["inference_accounting"] = cost_guard.snapshot()
    if dataset_digest(args.data) != provenance["dataset_sha256"]:
        raise RuntimeError("dataset changed during measurement; discard this run")
    return results


def _mean(values) -> float | None:
    values = [float(v) for v in values if v is not None]
    return round(statistics.mean(values), 4) if values else None


def summarize(rows, args, budget, ingest_s, recall_s, full_tokens, mode="memory", judge_info=None) -> dict:
    def block(rs):
        b_dict = {
            "n": len(rs),
            "evidence": _mean(r["evidence"] for r in rs),
            "complete": _mean(r["complete"] for r in rs),
            "session": _mean(r["session"] for r in rs),
            "answer_in_context": _mean(r["answer_in_context"] for r in rs),
            "tokens": _mean(r["tokens"] for r in rs),
            "context_text_tokens": _mean(r.get("context_text_tokens") for r in rs),
            "exact_budget_exceedances": (sum(r["context_text_tokens"] > budget for r in rs)
                                         if rs and all(r.get("context_text_tokens") is not None for r in rs)
                                         and mode in ("memory", "budgeted-history") else None),
            "unresolved_gold_turn_references": sum(len(r.get("unresolved_gold_turn_ids", [])) for r in rs),
            "recall_latency": _p50_p95([r.get("recall_latency_s") for r in rs]),
            "ranking": {
                level + "_" + metric: _mean(r.get(level + "_" + metric) for r in rs)
                for level in ("turn", "session") for metric in ("recall_at_5", "recall_at_10", "ndcg_at_10")
            },
            # Lexical completeness grader, alongside the evidence-id `complete` above.
            "completeness_score": _mean(r.get("completeness_score") for r in rs),
            "completeness": bucket_counts(r.get("completeness_bucket") for r in rs),
        }
        if args.answer:
            # Legacy downstream LoCoMo binary profile excludes category 5.
            if args.dataset == "locomo":
                scorable = [r for r in rs if is_scorable_category(r.get("type", ""))]
            else:
                scorable = rs
            b_dict["accuracy"] = _mean(r["correct"] for r in scorable if r.get("correct") is not None)
            b_dict["mean_score"] = _mean(r["score"] for r in scorable if r.get("score") is not None)
            b_dict["mean_context_tokens"] = _mean(r.get("context_tokens") for r in rs)
            b_dict["mean_answer_tokens_in"] = _mean(r.get("answer_tokens_in") for r in rs)
            b_dict["answer_latency"] = _p50_p95([r.get("answer_latency_s") for r in rs if not r.get("answer_cache_hit")])
            b_dict["judge_latency"] = _p50_p95([r.get("judge_latency_s") for r in rs if not r.get("judge_all_cached")])
            b_dict["historical_cached_cost_usd"] = sum(r.get("historical_cached_cost_usd", 0) for r in rs)
            b_dict["cache_hits"] = sum(c["cache_hit"] for r in rs for c in r.get("completion_calls", []))
            b_dict["total_cost_usd"] = round(
                sum(r.get("answer_cost_usd", 0.0) + r.get("judge_cost_usd", 0.0) for r in rs), 4
            )
            b_dict["answer_cost_usd"] = round(sum(r.get("answer_cost_usd", 0.0) for r in rs), 4)
            b_dict["judge_cost_usd"] = round(sum(r.get("judge_cost_usd", 0.0) for r in rs), 4)
            # Completeness/answer correlation: answer accuracy conditioned on the
            # retrieval context being lexically complete vs. not.
            complete_rows = [r for r in scorable if r.get("completeness_bucket") == "COMPLETE"]
            incomplete_rows = [r for r in scorable if r.get("completeness_bucket") in ("PARTIAL", "INSUFFICIENT")]
            b_dict["accuracy_when_complete"] = _mean(
                r["correct"] for r in complete_rows if r.get("correct") is not None)
            b_dict["accuracy_when_incomplete"] = _mean(
                r["correct"] for r in incomplete_rows if r.get("correct") is not None)
        else:
            b_dict["accuracy"] = None

        return b_dict

    by_type = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)

    summary = {
        "dataset": args.dataset,
        "budget": budget,
        "mode": mode,
        "embedder": args.embedder,
        "rerank": args.rerank,
        "neighbours": args.neighbours,
        "effective_embedders": sorted({tag for row in rows for tag in row.get("effective_embedders", [])}),
        "recall_timing": "independent public recall per budget; excludes measurement and inference",
        "context_reference": "entire-raw-history" if mode == "full-context" else mode,
        "overall": block(rows),
        "by_type": {t: block(rs) for t, rs in sorted(by_type.items())},
        "full_history_tokens": _mean(full_tokens),
        "ingest_seconds": round(ingest_s, 1),
        "recall_ms": round(1000 * recall_s, 1),
        "rows": rows,
    }
    if judge_info:
        summary.update(judge_info)

    if args.dataset == "beam" and args.answer:
        # BEAM official ability breakdown
        beam_scores = {}
        for ability in BEAM_ABILITIES:
            ability_rows = [r for r in rows if r.get("beam_ability") == ability or r.get("type") == ability]
            if ability_rows:
                beam_scores[ability] = _mean(r.get("score") for r in ability_rows if r.get("score") is not None)
        summary["beam_abilities"] = beam_scores

        eo_rows = [r for r in rows if r.get("beam_ability") == "event_ordering" or r.get("type") == "event_ordering"]
        if eo_rows:
            tau_bs = [r.get("beam_details", {}).get("tau_b") for r in eo_rows if isinstance(r.get("beam_details"), dict)]
            f1s = [r.get("beam_details", {}).get("f1") for r in eo_rows if isinstance(r.get("beam_details"), dict)]
            summary["beam_event_ordering"] = {
                "tau_b": _mean(tau_bs),
                "f1": _mean(f1s),
                "score": _mean(r.get("score") for r in eo_rows),
            }
        abs_rows = [r for r in rows if r.get("beam_ability") == "abstention" or r.get("type") == "abstention"]
        if abs_rows:
            summary["beam_abstention"] = {
                "accuracy": _mean(r.get("correct") for r in abs_rows if r.get("correct") is not None),
                "score": _mean(r.get("score") for r in abs_rows if r.get("score") is not None),
            }

    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dataset", choices=("locomo", "longmemeval", "dolphin", "beam"), required=True)
    p.add_argument("--personas", default="", help="dolphin: comma list (default: all three)")
    p.add_argument("--data", required=True)
    p.add_argument(
        "--root",
        default=os.path.join(tempfile.gettempdir(), "commontrace-conversation-benchmark"),
        help="store root, reused between runs so ingestion and embeddings are cached",
    )
    p.add_argument("--budget", default="1500", help="tokens; a comma list assembles each ranking at every budget")
    p.add_argument("--memory-adapter", choices=PROFILES, default="commontrace",
                   help="Optional local raw-source vendor profile (excludes managed APIs/LLM extraction)")
    p.add_argument("--embedder", default="auto", choices=("auto", "arctic-m", "minilm", "none"))
    p.add_argument("--rerank", default="auto", choices=("auto", "none", "cross-encoder", "cross-encoder-fast"))
    p.add_argument("--neighbours", type=int, default=1)
    p.add_argument(
        "--rerank-blend",
        type=float,
        default=1.0,
        help="cross-encoder weight beside the fused rank (0 lets it replace that rank)",
    )
    p.add_argument("--profile-facts", type=int, default=4)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--answer", action="store_true", help="answer and judge with COMMONTRACE_LLM_*")
    p.add_argument(
        "--chunk-set",
        default=None,
        help=("directory name under <root>/runs/chunk_sets/ for the parsed evaluation "
              "inputs (default: <dataset>-<hash>). Reused across sweeps when the "
              "dataset, limit, seed and data file are unchanged."),
    )
    p.add_argument(
        "--judge",
        choices=("auto", "generic", "longmemeval", "locomo", "beam"),
        default="auto",
        help="judge protocol to evaluate answers (default: auto matches dataset)",
    )
    p.add_argument(
        "--answer-model",
        default=None,
        help="model used to generate answers from context (default: from env or claude-sonnet-5)",
    )
    p.add_argument(
        "--judge-model",
        default=None,
        help="model used to judge answers (default: the configured judge profile's model)",
    )
    p.add_argument(
        "--modes",
        default="memory",
        help="comma list of reference modes: memory, full-context (entire history), budgeted-history, no-memory",
    )
    p.add_argument(
        "--max-cost",
        type=float,
        default=None,
        help="Reserve each bounded reader/judge request against this USD cap before dispatch; unknown charges abort.",
    )
    p.add_argument("--max-output-tokens", type=int, default=1536, help="Per-call output cap; applies to reader and every judge call.")
    p.add_argument("--tokenizer", default=None, help="Optional exact context text counting: tiktoken:<encoding-name>.")
    p.add_argument(
        "--cache-dir",
        default=None,
        help="directory for disk cache (default: <root>/benchmark_cache)",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="disable LLM disk caching",
    )
    p.add_argument(
        "--bootstrap",
        action="store_true",
        help="compute empirical 95%% bootstrap confidence intervals for all metrics",
    )
    p.add_argument(
        "--compare",
        default=None,
        help="path to another benchmark JSON output for paired bootstrap statistical comparison",
    )
    p.add_argument("--out")
    args = p.parse_args(argv)
    results = run(args)

    if args.compare:
        try:
            with open(args.compare) as fh:
                baseline_data = json.load(fh)
            for budget, result in results.items():
                b_str = str(budget)
                base_run = baseline_data.get(b_str, baseline_data)
                comp = paired_compare(base_run, result)
                result["comparison"] = comp
        except (OSError, ValueError, TypeError) as e:
            print(f"Comparison error: {e}", file=sys.stderr)
            return 2

    for budget, result in results.items():
        if "rows" in result and not args.out:
            result.pop("rows")
        if "modes_detail" in result and not args.out:
            for ms in result["modes_detail"].values():
                ms.pop("rows", None)
    print(
        json.dumps(
            {b: {k: v for k, v in r.items() if k not in ("rows", "modes_detail")} for b, r in results.items()},
            indent=2,
        )
    )
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=1, default=list)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
