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


def link_entities(root: str, fact, entities: list[tuple[str, str]]) -> list[str]:
    """Link local extraction to admitted facts without changing their source identity."""
    import re

    from commontrace import graph, ontology

    prescribed = ontology.load(root)
    linked = []
    with graph.batch(root):
        graph.add_node(root, fact.id, "memory", name=fact.statement[:200])
        for text, label in entities:
            # A model must cite a literal source span, not invent an entity.
            if text not in fact.statement:
                continue
            entity_type = prescribed.entity_type(label)
            node_id = prescribed.canonical_id(entity_type + ":" + re.sub(r"[^\w-]+", "_", text.casefold()))
            proof = {"kind": "gliner-local", "fact_id": fact.id, "quote": text}
            graph.add_node(root, node_id, entity_type, name=text, provenance=proof)
            graph.add_edge(root, node_id, fact.id, "mentions", valid_from=fact.valid_from,
                           invalid_at=fact.valid_until, expired_at=fact.expires_at, provenance=proof)
            linked.append(node_id)
    return linked
