import base64

import httpx
import pytest
from starlette.applications import Starlette

from hub import rest
from hub.abuse import make_rate_limiter
from hub.tests.test_rest import _key, _org_with_key
from unimem import oblivious

pytestmark = pytest.mark.asyncio


async def test_paired_hub_route_uses_authorized_clearance_not_request_payload(session_factory, config):
    centroids = {"cluster-a": [0.0]}
    records = [
        {"id": "public-row", "label": "public", "vector": [0.0], "text": "allowed"},
        {"id": "secret-row", "label": "restricted", "vector": [0.0], "text": "secret"},
    ]
    blocks = oblivious.cluster_records(records, centroids, clearance="public", block_bytes=512)
    replica = oblivious.XorPIRReplica(blocks, block_bytes=512)
    observed_clearances = []
    org_id, api_key = await _org_with_key(session_factory, name="PIR clearance")
    app = Starlette()
    rest.add_rest_routes(app, session_factory, config=config, rate_limiter=make_rate_limiter(config),
                         oblivious_clearance_resolver=lambda authenticated: observed_clearances.append(
                             authenticated.org_id) or "public",
                         oblivious_replica_resolver=lambda tenant, clearance: replica)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = _key(api_key)
        metadata = await client.post("/api/v1/oblivious/pir", headers=headers,
                                     json={"cluster": "cluster-a", "clearance": "restricted"})
        assert metadata.status_code == 200 and metadata.json()["count"] == oblivious.PUBLIC_CLUSTER_BLOCKS
        q0, q1 = oblivious.split_query(0, oblivious.PUBLIC_CLUSTER_BLOCKS)
        responses = []
        for share in (q0, q1):
            response = await client.post("/api/v1/oblivious/pir", headers=headers, json={
                "cluster": "cluster-a", "clearance": "restricted",
                "share": base64.b64encode(share).decode(),
            })
            assert response.status_code == 200
            responses.append(base64.b64decode(response.json()["answer"]))
    record = oblivious._decode_block(oblivious.reconstruct(*responses))
    assert record["id"] == "public-row"
    assert observed_clearances and set(observed_clearances) == {org_id}


async def test_pir_rejects_malformed_shares_and_offloads_replica_work(session_factory, config):
    import threading

    event_loop_thread = threading.get_ident()
    calls = []

    class Replica:
        def handle(self, cluster, share):
            calls.append(threading.get_ident())
            return {"count": 1, "block_bytes": 256}

        def handle_many(self, cluster, shares):
            calls.append(threading.get_ident())
            return [bytes(256)]

    _, api_key = await _org_with_key(session_factory, name="PIR validation")
    app = Starlette()
    rest.add_rest_routes(app, session_factory, config=config, rate_limiter=make_rate_limiter(config),
                         oblivious_clearance_resolver=lambda key: "public",
                         oblivious_replica_resolver=lambda tenant, clearance: Replica())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for body in ({"cluster": 3}, {"cluster": "a" * 97}, {"cluster": "a", "share": 123},
                     {"cluster": "a", "share": "a" * 176}, {"cluster": "a", "share": "!"},
                     {"cluster": "a", "shares": "AA=="},
                     {"cluster": "a", "share": "AA==", "shares": ["AA=="]}):
            response = await client.post("/api/v1/oblivious/pir", headers=_key(api_key), json=body)
            assert response.status_code == 400
        assert not calls
        for body in ({"cluster": "a"}, {"cluster": "a", "shares": ["AA=="]}):
            response = await client.post("/api/v1/oblivious/pir", headers=_key(api_key), json=body)
            assert response.status_code == 200
    assert len(calls) == 2 and all(thread != event_loop_thread for thread in calls)


async def test_deeply_nested_json_is_a_bad_request_instead_of_a_server_error():
    from starlette.requests import Request

    body = b'{"nested":' + b"[" * 4000 + b"0" + b"]" * 4000 + b"}"

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    assert await rest._read_json(Request({"type": "http", "method": "POST", "headers": []}, receive)) is None
