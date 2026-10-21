from __future__ import annotations

import json

import pytest


@pytest.mark.parametrize('operation,body,field', [
    ('outcome-correct', {'rows': [{'rate': .5, 'injected': True, 'prediction': .8, 'gold_probability': 1, 'gold': 1,
                                  'environment': 'real'}]}, 'estimate'),
    ('simulation-weight', {'condition': 'unknown'}, 'trust_weight'),
    ('recovery-recall', {'failure': {'failure_type': 'slip'}, 'context': {
        'site': 's', 'robot': 'r', 'embodiment': 'jaw', 'environment': 'sim'}}, 'recoveries'),
    ('data-value', {'episodes': []}, 'manifest'),
    ('underwriting', {}, 'manifest'),
    ('vendor-report', {}, 'stores'),
    ('royalties', {'offers': [], 'values': {}, 'budget_cents': 0}, 'payouts'),
])
def test_frontier_cli_requests(tmp_path, operation, body, field):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import cli
    path = tmp_path / 'request.json'
    path.write_text(json.dumps(body))
    result = cli('frontier', operation, str(path), '--dest', str(tmp_path / 'store'))
    assert result.returncode == 0, result.stderr
    assert field in json.loads(result.stdout)


def test_spatial_planner_history_and_belief_tools(tmp_path):
    from unimem import spatial
    from unimem.mcp_server import build_server
    pytest.importorskip("mcp")
    from tests.test_mcp_server import call
    context = {'tenant': 'a', 'site': 's', 'robot': 'r7'}
    spatial.update(str(tmp_path), {'object_id': 'box', 'class': 'crate', 'pose': [1, 2, 3],
        'frame': 'map', 'robot_id': 'r7', 'confidence': .8, 'timestamp': '2026-01-01T00:00:00Z'}, context=context)
    server = build_server(str(tmp_path))
    assert len(call(server, 'spatial_history', object_id='box', context=context)['history']) == 1
    from unimem import frontier_protocol as fp
    assert len(call(server, 'spatial_believed', robot='r7', time=fp.now(), context=context)['objects']) == 1
