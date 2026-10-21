"""A small learned typer: softmax regression over a mention's form and its context.

The weights are distilled offline from model-written, labeled sentences
(``benchmarks/train_ontology_typing.py``) and shipped with the package, so typing
needs no model at runtime. It predicts the built-in types plus ``none`` (an
abstract topic) and is one evidence source among the others in
``ontology_classify``: it only adds a score for a type the ontology declares.

Features are deliberately generic, so they transfer to names never seen:
character n-grams and the shape of the mention, its head word, and the words
just before and after it.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading

MODEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ontology_typing_model.json")
NONE = "none"

_WORD = re.compile(r"[a-z0-9]+(?:['.][a-z0-9]+)*")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_LOCK = threading.Lock()
_MODEL: Model | None = None
_LOADED = False


def _shape(name: str) -> str:
    """"Kaelen Voss" -> "Xx Xx", "requests.get" -> "x.x", "ERR_42" -> "X_d"."""
    out = []
    for ch in name:
        kind = "X" if ch.isupper() else "x" if ch.islower() else "d" if ch.isdigit() else ch
        if not out or out[-1] != kind:
            out.append(kind)
    return "".join(out)[:12]


def features(name: str, context: str) -> list[str]:
    """Sparse binary features for a mention in its context."""
    name = " ".join((name or "").split())
    lowered = name.lower()
    feats = [f"shape={_shape(name)}", f"ntok={min(len(name.split()), 4)}"]
    padded = f"^{lowered}$"
    for n in (3, 4):
        feats += [f"c{n}={padded[i:i + n]}" for i in range(len(padded) - n + 1)][:40]
    parts = _WORD.findall(_CAMEL.sub(" ", name).lower())
    if parts:
        feats += [f"head={parts[-1]}", f"first={parts[0]}"] + [f"part={p}" for p in parts[:6]]
    if name[:1].isupper():
        feats.append("cap")
    if "`" + name + "`" in (context or ""):
        feats.append("backtick")
    text = " ".join((context or "").split())
    at = text.lower().find(lowered) if lowered else -1
    if at >= 0:
        before = _WORD.findall(text[:at].lower())[-4:]
        after = _WORD.findall(text[at + len(name):].lower())[:4]
        opener = not text[:at].strip() or text[:at].rstrip()[-1:] in ".!?:"
        feats.append("start" if opener else "mid")
        for i, w in enumerate(reversed(before), 1):
            feats.append(f"L{i}={w}")
            feats.append(f"Lw={w}")
        for i, w in enumerate(after, 1):
            feats.append(f"R{i}={w}")
            feats.append(f"Rw={w}")
        if before and after:
            feats.append(f"L1R1={before[-1]}_{after[0]}")
        if len(after) >= 2:
            feats.append(f"R12={after[0]}_{after[1]}")
        if len(before) >= 2:
            feats.append(f"L21={before[-2]}_{before[-1]}")
    return feats


class Model:
    def __init__(self, labels: list[str], weights: dict[str, list[float]], bias: list[float], meta: dict):
        self.labels, self.weights, self.bias, self.meta = labels, weights, bias, meta

    def probabilities(self, name: str, context: str) -> dict[str, float]:
        scores = list(self.bias)
        for feat in set(features(name, context)):
            row = self.weights.get(feat)
            if row is not None:
                for i, w in enumerate(row):
                    scores[i] += w
        top = max(scores)
        exps = [math.exp(s - top) for s in scores]
        total = sum(exps)
        return {label: e / total for label, e in zip(self.labels, exps)}

    def to_dict(self) -> dict:
        return {"format": 1, "labels": self.labels, "bias": [round(b, 4) for b in self.bias],
                "weights": {f: [round(w, 4) for w in row] for f, row in sorted(self.weights.items())},
                "meta": self.meta}

    @classmethod
    def from_dict(cls, data: dict) -> Model:
        if data.get("format") != 1:
            raise ValueError("unsupported typing model format")
        return cls(list(data["labels"]), {k: list(v) for k, v in data["weights"].items()},
                   list(data["bias"]), dict(data.get("meta") or {}))


def train(rows: list[tuple[str, str, str]], *, epochs: int = 30, rate: float = 0.3, l2: float = 1e-4,
          min_count: int = 2, seed: int = 0) -> Model:
    """Softmax regression with Adagrad over (name, context, label) rows."""
    import random

    labels = sorted({label for _n, _c, label in rows})
    index = {label: i for i, label in enumerate(labels)}
    counts: dict[str, int] = {}
    encoded = []
    for name, context, label in rows:
        feats = sorted(set(features(name, context)))
        for f in feats:
            counts[f] = counts.get(f, 0) + 1
        encoded.append((feats, index[label]))
    kept = {f for f, c in counts.items() if c >= min_count}
    k = len(labels)
    weights = {f: [0.0] * k for f in kept}
    grads = {f: [1e-8] * k for f in kept}
    bias, bias_grad = [0.0] * k, [1e-8] * k
    order = list(range(len(encoded)))
    rnd = random.Random(seed)
    for _ in range(epochs):
        rnd.shuffle(order)
        for idx in order:
            feats, gold = encoded[idx]
            feats = [f for f in feats if f in kept]
            scores = list(bias)
            for f in feats:
                row = weights[f]
                for i in range(k):
                    scores[i] += row[i]
            top = max(scores)
            exps = [math.exp(s - top) for s in scores]
            total = sum(exps)
            for i in range(k):
                g = exps[i] / total - (1.0 if i == gold else 0.0)
                bias_grad[i] += g * g
                bias[i] -= rate * g / math.sqrt(bias_grad[i])
                for f in feats:
                    gw = g + l2 * weights[f][i]
                    grads[f][i] += gw * gw
                    weights[f][i] -= rate * gw / math.sqrt(grads[f][i])
    pruned = {f: row for f, row in weights.items() if max(abs(w) for w in row) >= 0.05}
    return Model(labels, pruned, bias, {"rows": len(rows), "features": len(pruned)})


def load() -> Model | None:
    """The shipped model, loaded once; None if it is absent or unreadable."""
    global _MODEL, _LOADED
    with _LOCK:
        if not _LOADED:
            _LOADED = True
            try:
                with open(MODEL_FILE, encoding="utf-8") as fh:
                    _MODEL = Model.from_dict(json.load(fh))
            except (OSError, ValueError, KeyError, TypeError):
                _MODEL = None
        return _MODEL
