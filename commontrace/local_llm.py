"""In-process language model: ``COMMONTRACE_LLM_PROVIDER=local``.

Runs an instruction-tuned Hugging Face model on this machine with
``transformers``, so lesson drafting, reflection and benchmark answering work
with no hosted credentials and no model server. Decoding is greedy, so the same
prompt and model give the same text. In offline mode (``--offline``) the
weights must already be in the local model cache; nothing is downloaded.

Small CPU models are slow and much weaker than hosted ones. Use this for
air-gapped deployments and for reproducible local measurement, not as a
stand-in for a frontier model's answer quality.
"""
from __future__ import annotations

import os
import threading

DEFAULT_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
DEFAULT_MAX_NEW_TOKENS = 512

_LOCK = threading.Lock()
_LOADED: dict[str, tuple] = {}


def _load(model: str):
    from commontrace import offline
    from commontrace.llm import LLMUnavailable

    with _LOCK:
        if model in _LOADED:
            return _LOADED[model]
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError:
            raise LLMUnavailable("COMMONTRACE_LLM_PROVIDER=local needs the optional transformers and torch "
                                 "packages: pip install transformers torch") from None
        cached_only = offline.enabled()
        try:
            tokenizer = AutoTokenizer.from_pretrained(model, local_files_only=cached_only)
            network = AutoModelForCausalLM.from_pretrained(model, local_files_only=cached_only,
                                                           dtype=torch.float32)
        except OSError as exc:
            hint = " (offline mode loads only cached weights)" if cached_only else ""
            raise LLMUnavailable(f"local model {model!r} could not be loaded{hint}: {exc}") from None
        network.eval()
        _LOADED[model] = (tokenizer, network, torch)
        return _LOADED[model]


def complete(config, prompt: str, *, max_new_tokens: int | None = None) -> tuple[str, dict]:
    """Greedy completion: (text, usage with exact input/output token counts)."""
    limit = max_new_tokens or int(os.environ.get("COMMONTRACE_LLM_MAX_TOKENS", DEFAULT_MAX_NEW_TOKENS))
    if limit < 1:
        raise ValueError("max_new_tokens must be positive")
    tokenizer, network, torch = _load(config.model)
    messages = [{"role": "user", "content": prompt}]
    if getattr(tokenizer, "chat_template", None):
        text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    else:
        text = prompt
    encoded = tokenizer(text, return_tensors="pt")
    input_tokens = int(encoded["input_ids"].shape[-1])
    with torch.inference_mode():
        output = network.generate(**encoded, max_new_tokens=limit, do_sample=False,
                                  pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id)
    generated = output[0][input_tokens:]
    answer = tokenizer.decode(generated, skip_special_tokens=True)
    return answer.strip(), {"input_tokens": input_tokens, "output_tokens": int(generated.shape[-1])}
