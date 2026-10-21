"""Personalized PageRank over the turn-entity graph (HippoRAG-style associative recall).

Every turn links to the entities it mentions. A random walk that restarts at the
question's entities and at the best lexical/dense matches spreads relevance along
shared entities, so a turn two associations away from the question can surface
even when it shares no words with it. That is the multi-hop case that keyword and
embedding scores miss.

The walk runs on a bounded local subgraph (the seeds' entities and the turns that
mention them), with each entity weighted by its specificity, so a name mentioned
everywhere carries almost nothing. It proposes candidates and scores; it never
asserts facts, and every turn it returns must still pass the normal evidence
screen, reranker and budget.
"""
from __future__ import annotations

import json
import math

DAMPING = 0.5          # HippoRAG's restart probability: half the mass returns to the seeds
ITERATIONS = 16
MAX_ENTITIES = 48      # entities taken from the seeds and the question
MAX_DEGREE = 64        # an entity in more turns than this names nothing in particular
MAX_TURNS = 800        # turns in the local subgraph


def personalized_pagerank(store, seeds: dict[int, float], question_entities=(), *, allowed=None,
                          question_weight: float = 1.0) -> dict[int, float]:
    """Stationary walk probability of each turn in the local subgraph around `seeds`.

    `seeds` maps turn id -> restart weight (for example its retrieval score);
    `question_entities` are names from the question, which also restart the walk.
    Returns {turn: probability} for turns in `allowed` (all when None)."""
    seeds = {t: w for t, w in seeds.items() if w > 0}
    names = [n.lower() for n in question_entities if n]
    if not seeds and not names:
        return {}
    db = store.db
    seed_entities: dict[str, set[int]] = {}
    if seeds:
        for name, turn in db.execute(
                "SELECT name, turn FROM entities WHERE turn IN (SELECT value FROM json_each(?))",
                (json.dumps(list(seeds)),)):
            seed_entities.setdefault(name, set()).add(turn)
    candidates = list(dict.fromkeys(names + sorted(seed_entities, key=lambda n: -len(seed_entities[n]))))
    if not candidates:
        return {}
    candidates = candidates[:MAX_ENTITIES * 2]
    degree = dict(db.execute(
        "SELECT name, COUNT(*) FROM entities WHERE name IN (SELECT value FROM json_each(?)) GROUP BY name",
        (json.dumps(candidates),)))
    kept = [n for n in candidates if 0 < degree.get(n, 0) <= MAX_DEGREE][:MAX_ENTITIES]
    if not kept:
        return {}
    # entity -> turns, bounded; specificity = 1 / log(2 + degree)
    edges: dict[str, list[int]] = {}
    total = 0
    for name, turn in db.execute(
            "SELECT name, turn FROM entities WHERE name IN (SELECT value FROM json_each(?)) ORDER BY name, turn",
            (json.dumps(kept),)):
        if allowed is not None and turn not in allowed:
            continue
        if total >= MAX_TURNS and turn not in seeds:
            continue
        edges.setdefault(name, []).append(turn)
        total += 1
    if not edges:
        return {}
    weight = {name: 1.0 / math.log(2 + degree[name]) for name in edges}
    turn_links: dict[int, list[str]] = {}
    for name, turns in edges.items():
        for turn in turns:
            turn_links.setdefault(turn, []).append(name)
    # restart distribution: seed turns by weight, question entities by question_weight
    restart: dict[object, float] = {}
    for turn, w in seeds.items():
        if turn in turn_links:
            restart[("t", turn)] = w
    for name in names:
        if name in edges:
            restart[("e", name)] = question_weight * weight[name]
    mass = sum(restart.values())
    if mass <= 0:
        return {}
    restart = {k: v / mass for k, v in restart.items()}
    # outgoing transition weights: turn -> its entities (by specificity), entity -> its turns (uniform)
    turn_out = {t: sum(weight[n] for n in links) for t, links in turn_links.items()}
    rank = dict(restart)
    for _ in range(ITERATIONS):
        nxt: dict[object, float] = {k: (1 - DAMPING) * v for k, v in restart.items()}
        for key, value in rank.items():
            if value <= 0:
                continue
            kind, node = key
            if kind == "t":
                links = turn_links.get(node)
                if not links:
                    continue
                share = DAMPING * value / turn_out[node]
                for name in links:
                    nxt[("e", name)] = nxt.get(("e", name), 0.0) + share * weight[name]
            else:
                turns = edges.get(node)
                if not turns:
                    continue
                share = DAMPING * value / len(turns)
                for turn in turns:
                    nxt[("t", turn)] = nxt.get(("t", turn), 0.0) + share
        rank = nxt
    return {node: value for (kind, node), value in rank.items() if kind == "t" and value > 0}
