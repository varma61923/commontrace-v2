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


def test_joint_judge_drives_temporal_supersede_and_preserves_old_valid_time(tmp_path, monkeypatch):
    monkeypatch.setenv('COMMONTRACE_FACT_CONFLICT_JUDGE', 'llm')
    monkeypatch.setenv('COMMONTRACE_FACT_CONFLICTS', 'supersede')
    monkeypatch.setenv('COMMONTRACE_FACT_DEDUP', 'exact')
    calls = []

    def complete(prompt):
        offered = json.loads(prompt.rsplit('\n', 1)[1])['existing']
        calls.append(offered)
        fact_id = next(iter(offered))
        return json.dumps({'duplicates': [], 'contradictions': [fact_id]}), {}

    monkeypatch.setattr('commontrace.llm.complete', complete)
    root = str(tmp_path)
    old, _ = hierarchical.add_fact(root, 'Alice likes spicy food a lot', valid_from='2024-01-01')
    new, _ = hierarchical.add_fact(root, 'Alice does not like spicy food a lot', valid_from='2025-01-01')

    stored = hierarchical.load_facts(root)[old.id]
    assert calls == [{old.id: old.statement}]
    assert stored.status == 'superseded' and stored.superseded_by == new.id
    assert stored.valid_until == '2025-01-01T00:00:00+00:00' and stored.retracted_at
    assert [fact.id for fact in hierarchical.list_facts(root, as_of='2024-06-01')] == [old.id]
    assert [fact.id for fact in hierarchical.list_facts(root, as_of='2025-06-01')] == [new.id]


def test_exact_entity_tier_needs_no_llm(tmp_path):
    root = str(tmp_path)
    graph.add_node(root, id='entity:a', entity_type='entity', name='ParserTool')
    graph.add_node(root, id='entity:b', entity_type='entity', name='ParserTool')
    result = entity_resolution.tiered_candidates(root)
    assert result['pairs'][0]['tier'] == 'exact'
    assert result['llm_batches'] == 0
