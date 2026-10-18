import base64

import httpx
import pytest
from starlette.applications import Starlette

from commontrace import oblivious
from hub import rest
from hub.abuse import make_rate_limiter
from hub.tests.test_rest import _key, _org_with_key

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
