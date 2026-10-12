"""Acceptance checks for the automatic profile plus search turn operation."""

from __future__ import annotations

import json

from commontrace import fact_store
from commontrace.client import MemoryClient
from commontrace.gateway import Gateway
from commontrace.memory_journal import Journal
from commontrace.memory_record import MemoryRecord

TOKEN = "p" * 40


def test_one_gateway_turn_returns_profile_buckets_and_retrieval(tmp_path):
    root = str(tmp_path)
    context = {"user": "profile-user", "agent": "writer"}
    client = MemoryClient(root, context=context)
    fact = client.add("The quasar checksum job keeps each archive for 17 days.", local=True)["facts"][0]
    assert not fact_store.enabled(root)
    Journal(root).put(
        MemoryRecord(
            "custom-profile",
            "profile",
            1,
            {"text": "Use terse answers", "bucket": "custom"},
            scope={"agent": "writer", "user": "profile-user"},
        )
    )

    gateway = Gateway(root, token=TOKEN, durable=False)
    response = gateway.handle(
        "POST",
        "/v1/memory/turn",
        {"Host": "localhost", "Authorization": "Bearer " + TOKEN},
        json.dumps(
            {
                "session_id": "profile-search-acceptance",
                "query": "quasar checksum archive retention 17 days",
                "text": "I am a research engineer. I prefer append-only stores.",
                "context": ["agent:writer", "user:profile-user"],
            }
        ).encode(),
    )

    assert response.status == 200
    result = json.loads(response.body)
    profile = result["profile"]
    assert any(row["text"] == "I am a research engineer" for row in profile["static"])
    assert any(row["text"] == "I prefer append-only stores" for row in profile["dynamic"])
    assert any(row["text"] == "Use terse answers" for row in profile["custom"])
    assert any(row["id"] == fact["id"] for row in result["results"])
