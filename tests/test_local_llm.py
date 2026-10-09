"""In-process `local` LLM provider and the deterministic exact judge."""
import sys
import types

import pytest

from benchmarks.cache import CostGuard
from benchmarks.judges import ExactJudge, get_judge
from benchmarks.judges.exact import token_f1
from benchmarks.requests import bounded_complete
from commontrace import llm, local_llm


def test_local_provider_needs_no_key_and_defaults_the_model(monkeypatch):
    monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "local")
    monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("COMMONTRACE_LLM_MODEL", raising=False)
    cfg = llm.load_config()
    assert cfg.provider == "local" and cfg.model == local_llm.DEFAULT_MODEL and cfg.api_key == ""
    monkeypatch.setenv("COMMONTRACE_LLM_MODEL", "org/tiny")
    assert llm.load_config().model == "org/tiny"


def test_complete_dispatches_to_the_in_process_model(monkeypatch):
    seen = {}

    def fake(config, prompt, *, max_new_tokens=None):
        seen.update(model=config.model, prompt=prompt, limit=max_new_tokens)
        return "Paris", {"input_tokens": 7, "output_tokens": 1}

    monkeypatch.setattr(local_llm, "complete", fake)
    monkeypatch.delenv("COMMONTRACE_LLM_CACHE", raising=False)
    text, usage = llm.complete("capital of France?", llm.Config(provider="local", model="org/tiny", api_key=""))
    assert text == "Paris" and usage["output_tokens"] == 1 and seen["model"] == "org/tiny"


def test_bounded_benchmark_request_costs_nothing_and_keeps_the_output_cap(monkeypatch):
    monkeypatch.setattr(local_llm, "complete",
                        lambda config, prompt, *, max_new_tokens=None: ("ok", {"input_tokens": 3,
                                                                                "output_tokens": max_new_tokens}))
    guard = CostGuard(max_cost_usd=0.0)
    cfg = llm.Config(provider="local", model="org/tiny", api_key="")
    text, usage, cost = bounded_complete("q", cfg, guard, output_limit=16)
    assert (text, cost, usage["output_tokens"], guard.call_count) == ("ok", 0.0, 16, 1)
    monkeypatch.setattr(local_llm, "complete",
                        lambda config, prompt, *, max_new_tokens=None: ("x", {"input_tokens": 3, "output_tokens": 99}))
    with pytest.raises(ValueError, match="output limit"):
        bounded_complete("q", cfg, guard, output_limit=16)


def _fake_runtime(monkeypatch, calls):
    class Tensor:
        def __init__(self, ids):
            self.ids = ids
            self.shape = (1, len(ids)) if ids and isinstance(ids[0], int) else (len(ids),)

        def __getitem__(self, key):
            if isinstance(key, slice):
                return Tensor(self.ids[key])
            return Tensor(self.ids)

    class Tokenizer:
        chat_template = "x"
        pad_token_id = None
        eos_token_id = 0

        @classmethod
        def from_pretrained(cls, name, local_files_only):
            calls.append(("tokenizer", name, local_files_only))
            return cls()

        def apply_chat_template(self, messages, tokenize, add_generation_prompt):
            return "<user>" + messages[0]["content"]

        def __call__(self, text, return_tensors):
            return {"input_ids": Tensor([1, 2, 3])}

        def decode(self, ids, skip_special_tokens):
            return " answer "

    class Model:
        @classmethod
        def from_pretrained(cls, name, local_files_only, dtype):
            calls.append(("model", name, local_files_only))
            return cls()

        def eval(self):
            return self

        def generate(self, input_ids, max_new_tokens, do_sample, pad_token_id):
            calls.append(("generate", max_new_tokens, do_sample))
            return [Tensor([1, 2, 3, 9, 9])]

    class _NoGrad:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(float32="f32", inference_mode=_NoGrad))
    monkeypatch.setitem(sys.modules, "transformers",
                        types.SimpleNamespace(AutoTokenizer=Tokenizer, AutoModelForCausalLM=Model))
    monkeypatch.setattr(local_llm, "_LOADED", {})


def test_greedy_generation_counts_tokens_and_offline_loads_cache_only(monkeypatch):
    calls = []
    _fake_runtime(monkeypatch, calls)
    monkeypatch.setenv("COMMONTRACE_OFFLINE", "1")
    cfg = llm.Config(provider="local", model="org/tiny", api_key="")
    text, usage = local_llm.complete(cfg, "hi", max_new_tokens=8)
    assert text == "answer" and usage == {"input_tokens": 3, "output_tokens": 2}
    assert ("tokenizer", "org/tiny", True) in calls and ("model", "org/tiny", True) in calls
    assert ("generate", 8, False) in calls
    local_llm.complete(cfg, "again", max_new_tokens=8)
    assert sum(1 for c in calls if c[0] == "model") == 1  # weights load once per process


def test_missing_runtime_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "transformers", None)
    monkeypatch.setattr(local_llm, "_LOADED", {})
    with pytest.raises(llm.LLMUnavailable, match="transformers"):
        local_llm.complete(llm.Config(provider="local", model="org/none", api_key=""), "hi")


def test_exact_judge_credits_containment_and_overlap_only():
    judge = get_judge("exact")
    assert isinstance(judge, ExactJudge)
    q = {"question": "Where?", "answer": "the Louvre museum", "answers": ["the Louvre museum", "Louvre"]}
    assert judge.grade(q, "She went to the Louvre.")["correct"] is True
    assert judge.grade(q, "Musee du Louvre museum")["correct"] is True
    assert judge.grade(q, "She stayed home.")["correct"] is False
    assert judge.grade({"question": "n", "answer": "7"}, "17 people")["correct"] is False  # whole tokens only
    assert token_f1("a cat", "the cat") == 1.0
