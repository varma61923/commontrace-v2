from __future__ import annotations

import os

from commontrace import frontmatter, graph, retrieval


def test_t3_graph_boost_lesson_retrieval_lifecycle(isolated_store: str):
    lessons_dir = os.path.join(isolated_store, "memory", "lessons")
    os.makedirs(lessons_dir, exist_ok=True)

    l1_path = os.path.join(lessons_dir, "lesson_generic_retry.md")
    frontmatter.write(
        l1_path,
        {
            "name": "lesson_generic_retry",
            "description": "Retry network requests on transient failures",
            "applies_when": "Network timeout",
            "tags": ["network", "retry"],
            "status": "active",
        },
        "Retry transient network requests up to 3 times.",
    )

    l2_path = os.path.join(lessons_dir, "lesson_stripe_backoff.md")
    frontmatter.write(
        l2_path,
        {
            "name": "lesson_stripe_backoff",
            "description": "Handle Stripe rate limits with jittered exponential backoff",
            "applies_when": "HTTP 429 Too Many Requests",
            "tags": ["stripe", "billing", "rate-limit"],
            "status": "active",
        },
        "Handle HTTP 429 errors from Stripe by sleeping with randomized jitter.",
    )

    graph.add_node(isolated_store, "service:stripe", entity_type="service", name="Stripe API")
    graph.add_node(isolated_store, "lesson:lesson_stripe_backoff", entity_type="lesson", name="Stripe Backoff")
    graph.add_edge(isolated_store, "service:stripe", "lesson:lesson_stripe_backoff", "resolves", weight=1.0)

    task = "Investigate HTTP 429 errors when sending requests to Stripe API"
    candidate_slugs = ["lesson_generic_retry", "lesson_stripe_backoff"]
    boosts = graph.graph_boost_for_lessons(isolated_store, task, candidate_slugs)

    assert boosts.get("lesson_stripe_backoff", 0.0) > 0.0

    lessons = [
        (l1_path, frontmatter.read(l1_path)[0]),
        (l2_path, frontmatter.read(l2_path)[0]),
    ]

    ranked = retrieval.rank_lessons(
        task, lessons,
        graph_boost_lookup=boosts,
        graph_weight=1.0,
        adaptive_tail=False,
        floor=0.0,
    )
    assert len(ranked) >= 2
    assert ranked[0].slug == "lesson_stripe_backoff", "Graph-boosted lesson must rank #1"
