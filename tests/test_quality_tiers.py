import json

from commontrace import entity_resolution, fact_conflicts, graph, hierarchical


def test_joint_judgment_bounds_and_disjoint_decisions(tmp_path):
    fact, _ = hierarchical.add_fact(str(tmp_path), 'Alice works at A')
    def judge(prompt):
        return json.dumps({'duplicates': [fact.id, 'invented'], 'contradictions': []}), {}
    assert fact_conflicts.judge_joint('Alice is employed by A', [fact], complete=judge)['duplicates'] == {fact.id}
    def ambiguous(prompt):
        return json.dumps({'duplicates': [fact.id], 'contradictions': [fact.id]}), {}
    assert not fact_conflicts.judge_joint('new', [fact], complete=ambiguous)['contradictions']


def test_exact_entity_tier_needs_no_llm(tmp_path):
    root = str(tmp_path)
    graph.add_node(root, id='entity:a', entity_type='entity', name='ParserTool')
    graph.add_node(root, id='entity:b', entity_type='entity', name='ParserTool')
    result = entity_resolution.tiered_candidates(root)
    assert result['pairs'][0]['tier'] == 'exact'
    assert result['llm_batches'] == 0
