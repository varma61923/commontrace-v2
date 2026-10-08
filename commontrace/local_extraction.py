"""Optional local GLiNER adapter; model loading is explicitly offline."""
from __future__ import annotations

import os


def gliner_entities(text: str, labels: list[str], *, model=None, model_path: str | None = None,
                    threshold: float = 0.5) -> list[tuple[str, str]]:
    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be in [0, 1]")
    if model is None:
        if not model_path or not os.path.isdir(model_path):
            raise ValueError("GLiNER needs an existing local model directory")
        try:
            from gliner import GLiNER
        except ImportError:
            raise RuntimeError("install GLiNER separately and supply local model weights") from None
        model = GLiNER.from_pretrained(model_path, local_files_only=True)
    entities = model.predict_entities(text, labels, threshold=threshold)
    return sorted({(row["text"], row["label"]) for row in entities
                   if row.get("label") in labels and row.get("text") and row.get("score", 0) >= threshold})
