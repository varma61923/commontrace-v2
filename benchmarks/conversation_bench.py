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
import time
from collections import defaultdict
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.bootstrap import bootstrap_ci, compare_runs, format_comparison_markdown  # noqa: E402
from benchmarks.cache import BenchmarkCache, CostGuard, compute_cost_usd  # noqa: E402
from benchmarks.completeness import bucket_counts, grade_context_completeness  # noqa: E402
from benchmarks.judges import (  # noqa: E402
    BEAM_ABILITIES,
    get_default_judge_for_dataset,
    get_judge,
    is_scorable_category,
)
from commontrace.conversation import Options, Store, recall  # noqa: E402
from commontrace.conversation.search import assemble, tokens  # noqa: E402

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
            "limit": int(limit or 0), "seed": int(seed or 0), "personas": personas or ""}


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
        return list(locomo_cases(args.data))
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
    fdir = os.path.join(args.root, "runs", "chunk_sets", name)
    manifest_path = os.path.join(fdir, "manifest.json")
    chunks_path = os.path.join(fdir, "chunks.jsonl")

    if os.path.isfile(manifest_path) and os.path.isfile(chunks_path):
        try:
            manifest = json.load(open(manifest_path, encoding="utf-8"))
        except (ValueError, OSError):
            manifest = None
        if manifest and manifest.get("fingerprint") == fingerprint:
            with open(chunks_path, encoding="utf-8") as fh:
                payload = [json.loads(line) for line in fh if line.strip()]
            info = dict(manifest.get("info") or {})
            info.update({"name": name, "path": fdir, "reused": True, "layout": CHUNK_SET_LAYOUT})
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
            "questions": sum(len(qs) for _s, _sess, _n, qs in cases)}
    manifest = {"fingerprint": fingerprint, "info": info, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "layout": CHUNK_SET_LAYOUT}
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
        refs = {m["id"] for _s, _d, ms in sessions for m in ms}
        questions = []
        for i, qa in enumerate(conv["qa"]):
            if qa.get("category") not in LOCOMO_CATEGORIES:
                continue
            gold = {e.strip() for e in qa.get("evidence", []) if e.strip() in refs}
            questions.append({"id": f"{conv['sample_id']}-{i}", "question": qa["question"],
                              "answer": str(qa.get("answer", "")), "type": LOCOMO_CATEGORIES[qa["category"]],
                              "evidence": gold, "sessions": set()})
        yield conv["sample_id"], sessions, None, questions


def longmemeval_cases(path: str, limit: int, seed: int):
    data = json.load(open(path))
    if limit:
        by_type = defaultdict(list)
        for q in data:
            by_type[q["question_type"]].append(q)
        rng = random.Random(seed)
        share = max(1, limit // len(by_type))
        data = [q for t in sorted(by_type) for q in rng.sample(by_type[t], min(share, len(by_type[t])))]
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
        yield q["question_id"], sessions, q["question_date"], [question]


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
        refs = {m["id"] for _s, _d, ms in sessions for m in ms}
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
                    "evidence": _ids(q.get("source_chat_ids")) & refs,
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


def make_full_context(sessions: list, budget: int) -> tuple[str, int]:
    """Assemble chronological raw session history up to budget tokens."""
    blocks = []
    spent = 0
    for session, date, messages in sessions:
        header = f"=== {session} ({date}) ===" if date else f"=== {session} ==="
        header_tok = tokens(header) + 1
        lines = [header]
        spent += header_tok
        if spent > budget:
            break
        for m in messages:
            speaker = m.get("speaker") or m.get("role") or "speaker"
            text = m.get("text") or ""
            line = f"{speaker}: {text}"
            cost = tokens(line) + 1
            if spent + cost > budget:
                break
            lines.append(line)
            spent += cost
        blocks.append("\n".join(lines))
        if spent >= budget:
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
        )
    except Exception:
        provider = os.environ.get("COMMONTRACE_LLM_PROVIDER", "openai-compatible")
        api_key = os.environ.get("COMMONTRACE_LLM_API_KEY", "")
        base_url = os.environ.get("COMMONTRACE_LLM_BASE_URL", "")
        return llm.Config(provider=provider, model=model, api_key=api_key, base_url=base_url or None)


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
) -> dict:
    """Generate answer from context with ans_model and grade with judge_inst using j_model."""
    from commontrace import llm

    def call_cached(prompt: str, model: str) -> tuple[str, dict, float, float]:
        t0 = time.time()
        if cache is not None:
            hit = cache.get(model, prompt)
            if hit is not None:
                resp, usage, cost = hit
                if cost_guard:
                    cost_guard.record_call(cost, is_cached=True)
                return resp, usage, 0.0, cost

        cfg = _get_llm_config(model)
        resp, usage = llm.complete(prompt, config=cfg)
        latency = time.time() - t0
        cost = compute_cost_usd(usage, model)
        if cost_guard:
            cost_guard.record_call(cost, is_cached=False)
        if cache is not None:
            cache.put(model, prompt, resp, usage, cost)
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
    ans_text, ans_usage, ans_lat, ans_cost = call_cached(ans_prompt, ans_model)

    # Step 2: Judge answer
    judge_latencies = []
    judge_costs = []

    def judge_complete(prompt: str) -> tuple[str, dict]:
        resp, usage, lat, c = call_cached(prompt, j_model)
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
        "context_tokens": tokens(context) if context else 0,
        "answer_latency_s": ans_lat,
        "judge_latency_s": j_lat_total,
        "answer_cost_usd": ans_cost,
        "judge_cost_usd": j_cost_total,
        "judge": judge_inst.name,
        "judge_model": j_model,
        "answer_model": ans_model,
    }
    if "ability" in grade_res:
        res["beam_ability"] = grade_res["ability"]
    if "details" in grade_res:
        res["beam_details"] = grade_res["details"]
    return res


def run(args) -> dict:
    os.makedirs(args.root, exist_ok=True)
    budgets = [int(b) for b in str(args.budget).split(",")]
    modes = [m.strip() for m in getattr(args, "modes", "memory").split(",") if m.strip()]
    if not modes:
        modes = ["memory"]

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

        # Pre-flight cost check if --max-cost is passed
        if cost_guard.max_cost_usd is not None:
            dataset_q_counts = {"locomo": 1540, "longmemeval": 500, "beam": 400, "dolphin": 600}
            est_q = args.limit if args.limit else dataset_q_counts.get(args.dataset, 500)
            cost_guard.check_estimate(
                num_questions=est_q * len(budgets) * len(modes),
                answer_model=ans_model,
                judge_model=j_model,
                avg_context_tokens=budgets[0],
            )

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

    rows_by_mode = {m: {b: [] for b in budgets} for m in modes}
    ingest_s, recall_s, full_tokens = 0.0, 0.0, []

    for space, sessions, now, questions in cases:
        with Store(args.root, re.sub(r"[^A-Za-z0-9._-]", "_", space)) as store:
            t = time.time()
            if store.stats()["turns"] < sum(len(ms) for _s, _d, ms in sessions):
                for session, date, messages in sessions:
                    store.add(session, messages, session_at=date)
            ingest_s += time.time() - t
            full_tokens.append(sum(tokens(m["text"]) for _s, _d, ms in sessions for m in ms))

            for q in questions:
                t = time.time()
                r = recall(store, q["question"], now=now, options=opts)
                recall_s += time.time() - t

                # Compute memory contexts for each budget
                mem_contexts = {}
                for budget in budgets:
                    if budget != budgets[0]:
                        opts_b = Options(**{**opts.__dict__, "budget": budget})
                        context, used, n_tokens = assemble(store, q["question"], r.ranked, opts_b)
                    else:
                        context, used, n_tokens = r.context, r.turns, r.tokens
                    kept = store.turns(used).values()
                    refs, sess = {tt.ref for tt in kept}, {tt.session for tt in kept}
                    mem_contexts[budget] = (context, used, n_tokens, refs, sess)

                for mode in modes:
                    for budget in budgets:
                        mem_context, used, n_tokens, refs, sess = mem_contexts[budget]
                        if mode == "memory":
                            ctx = mem_context
                            toks = n_tokens
                            ev = (len(q["evidence"] & refs) / len(q["evidence"])) if q["evidence"] else None
                            comp = q["evidence"] <= refs if q["evidence"] else None
                            ses = (len(q["sessions"] & sess) / len(q["sessions"])) if q["sessions"] else None
                            ans_in_ctx = answer_in(ctx, q["answer"])
                            conf = r.explain.get("confidence")
                            rtop = r.explain.get("rerank_top")
                        elif mode == "full-context":
                            ctx, toks = make_full_context(sessions, budget)
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
                            "tokens": toks,
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
                            )
                            row.update(grade_info)
                        rows_by_mode[mode][budget].append(row)

        if args.limit and args.dataset == "locomo" and len(rows_by_mode[modes[0]][budgets[0]]) >= args.limit:
            break

    n = max(1, len(rows_by_mode[modes[0]][budgets[0]]))
    judge_info = {
        "judge": j_name,
        "judge_model": j_model,
        "answer_model": ans_model,
    } if args.answer else {}

    results = {}
    for b in budgets:
        if len(modes) == 1:
            m = modes[0]
            results[b] = summarize(
                rows_by_mode[m][b], args, b, ingest_s, recall_s / n, full_tokens, mode=m, judge_info=judge_info
            )
            results[b]["chunk_set"] = chunk_info
        else:
            mode_summaries = {
                m: summarize(
                    rows_by_mode[m][b], args, b, ingest_s, recall_s / n, full_tokens, mode=m, judge_info=judge_info
                )
                for m in modes
            }
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
            # Lexical completeness grader, alongside the evidence-id `complete` above.
            "completeness_score": _mean(r.get("completeness_score") for r in rs),
            "completeness": bucket_counts(r.get("completeness_bucket") for r in rs),
        }
        if args.answer:
            # LoCoMo protocol: category 5 (adversarial) is excluded from overall accuracy
            if args.dataset == "locomo":
                scorable = [r for r in rs if is_scorable_category(r.get("type", ""))]
            else:
                scorable = rs
            b_dict["accuracy"] = _mean(r["correct"] for r in scorable if r.get("correct") is not None)
            b_dict["mean_score"] = _mean(r["score"] for r in scorable if r.get("score") is not None)
            b_dict["mean_context_tokens"] = _mean(r.get("context_tokens") for r in rs)
            b_dict["mean_answer_tokens_in"] = _mean(r.get("answer_tokens_in") for r in rs)
            b_dict["answer_latency"] = _p50_p95([r.get("answer_latency_s") for r in rs])
            b_dict["judge_latency"] = _p50_p95([r.get("judge_latency_s") for r in rs])
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

        if getattr(args, "bootstrap", False):
            # Compute empirical 95% bootstrap confidence intervals
            b_dict["bootstrap_95ci"] = {
                "evidence": bootstrap_ci([r["evidence"] for r in rs if r.get("evidence") is not None]),
                "complete": bootstrap_ci([r["complete"] for r in rs if r.get("complete") is not None]),
                "answer_in_context": bootstrap_ci([r["answer_in_context"] for r in rs if r.get("answer_in_context") is not None]),
                "tokens": bootstrap_ci([r["tokens"] for r in rs if r.get("tokens") is not None]),
            }
            if args.answer:
                b_dict["bootstrap_95ci"]["accuracy"] = bootstrap_ci(
                    [r["correct"] for r in scorable if r.get("correct") is not None]
                )
                b_dict["bootstrap_95ci"]["mean_score"] = bootstrap_ci(
                    [r["score"] for r in scorable if r.get("score") is not None]
                )
            b_dict["bootstrap_95ci"]["completeness_score"] = bootstrap_ci(
                [r["completeness_score"] for r in rs if r.get("completeness_score") is not None]
            )
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
        default=os.path.join("benchmarks", ".work"),
        help="store root, reused between runs so ingestion and embeddings are cached",
    )
    p.add_argument("--budget", default="1500", help="tokens; a comma list assembles each ranking at every budget")
    p.add_argument("--embedder", default="arctic-m", choices=("arctic-m", "minilm", "none"))
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
        help="model used to judge answers (default: official model for the benchmark)",
    )
    p.add_argument(
        "--modes",
        default="memory",
        help="comma list of reference modes: memory, full-context, no-memory (default: memory)",
    )
    p.add_argument(
        "--max-cost",
        type=float,
        default=None,
        help="pre-flight check: maximum allowed estimated cost in USD; aborts before model calls if exceeded",
    )
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
                comp = compare_runs(result, base_run)
                result["comparison"] = comp
                print(format_comparison_markdown(comp))
        except Exception as e:
            print(f"Comparison error: {e}", file=sys.stderr)

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
