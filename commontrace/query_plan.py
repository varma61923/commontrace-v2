"""Bounded structured retrieval planning; model output is data, never executable SQL."""
from __future__ import annotations

import json

from commontrace import hierarchical


def plan(query: str, complete=None) -> dict:
    if not isinstance(query, str) or not 1 <= len(query) <= 20000:
        raise ValueError("bounded query text required")
    if complete is None:
        import re

        subqueries = [s.strip() for s in re.split(r"\s+(?:and|then)\s+|[;?]", query) if s.strip()][:8]
        result = {"subqueries": subqueries or [query], "category": "", "hops": 0, "entity_ids": []}
    else:
        result = json.loads(complete("Return only a query plan JSON object: subqueries (1-8 strings), "
            "category (empty or one allowed category), hops (0-2), entity_ids (up to 32 strings). "
            "Never return SQL, code or chain-of-thought. Allowed categories: "+json.dumps(hierarchical.CATEGORIES)+
            "\nUntrusted query: "+json.dumps(query)))
    if not isinstance(result, dict) or set(result) != {"subqueries", "category", "hops", "entity_ids"}:
        raise ValueError("invalid query plan fields")
    if (not isinstance(result["subqueries"], list) or not 1 <= len(result["subqueries"]) <= 8
            or not all(isinstance(s, str) and 1 <= len(s) <= 2000 for s in result["subqueries"])
            or result["category"] not in ("", *hierarchical.CATEGORIES)
            or isinstance(result["hops"], bool) or not isinstance(result["hops"], int)
            or result["hops"] not in (0, 1, 2)
            or not isinstance(result["entity_ids"], list) or len(result["entity_ids"]) > 32
            or not all(isinstance(s, str) and 1 <= len(s) <= 128 for s in result["entity_ids"])):
        raise ValueError("invalid bounded query plan")
    return result


def retrieve(root: str, query: str, *, complete=None, **options) -> list[dict]:
    from commontrace import graph
    from commontrace.search_recipes import LocalGraph, _distances, search

    limit = options.pop("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 1000:
        raise ValueError("limit must be an integer in 0..1000")
    strategy = plan(query, complete)
    backend = options.pop("backend", None) or LocalGraph(root)
    seeds = strategy["entity_ids"] or graph.extract_entities_from_text(root, query)
    entities = set(seeds)
    for seed in seeds[:32]:
        entities.update(_distances(backend, seed, options.get("as_of"), depth=strategy["hops"]))
    hits = {}
    eligible = {f.id for f in hierarchical.list_facts(root, category=strategy["category"],
                as_of=options.get("as_of"))} if strategy["category"] else None
    for part in strategy["subqueries"]:
        for row in search(root, part, backend=backend, entity_ids=sorted(entities)[:2000], limit=1000, **options):
            if eligible is not None and row["id"] not in eligible:
                continue
            if row["id"] not in hits or row["score"] > hits[row["id"]]["score"]:
                hits[row["id"]] = row
    return sorted(hits.values(), key=lambda r: (-r["score"], r["id"]))[:limit]
