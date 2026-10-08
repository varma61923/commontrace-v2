"""Real-file snapshot isolation and current trust for selective governed search."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from commontrace import explorer, frontmatter, gateway, hierarchical, lesson_admission, recall
from commontrace.conversation.coverage import assess
from commontrace.fact_evidence import EvidenceResolver, bind_evidence

SCORERS = ("overlap-v1", "bm25-v1")


def add(root: Path, statement: str, **kwargs):
    return hierarchical.add_fact(str(root), statement, valid_from="2026-01-01T00:00:00Z", **kwargs)[0]


def search(root: Path, query: str = "Lumen timeout", **kwargs):
    return hierarchical.search_facts(str(root), query, **kwargs)


def file_path(root: Path) -> Path:
    return Path(root, "memory", "facts", "facts.jsonl")


def rewrite_in_another_process(path: Path, old: str, new: str, *, atomic: bool) -> None:
    """Change actual equal-size bytes while restoring mtime, without sleeps."""
    script = """
import os, sys
path, old, new, atomic = sys.argv[1:]
before = os.stat(path)
with open(path, 'r', encoding='utf-8') as source:
    content = source.read()
assert old in content and len(old.encode()) == len(new.encode())
target = path + '.next' if atomic == '1' else path
with open(target, 'w', encoding='utf-8') as output:
    output.write(content.replace(old, new))
    output.flush()
    os.fsync(output.fileno())
os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
if atomic == '1':
    os.replace(target, path)
"""
    subprocess.run([sys.executable, "-c", script, str(path), old, new, "1" if atomic else "0"],
                   check=True, capture_output=True, text=True)


@pytest.mark.parametrize("scorer", SCORERS)
def test_foreign_scope_documents_do_not_change_visible_statistics(tmp_path, scorer):
    root = tmp_path
    add(root, "Lumen timeout is five seconds", scopes=["alpha"], confidence=0.8)
    add(root, "Lumen retries have an independent timeout constraint", scopes=["alpha"], confidence=0.9)
    public = add(root, "The Lumen service has a timeout monitor", confidence=0.7)
    before = [(fact.id, score) for fact, score in search(root, scope="alpha", scorer=scorer)]
    assert public.id in {identity for identity, _ in before}
    hierarchical.add_facts(str(root), [
        {"statement": f"Lumen timeout timeout timeout beta-only marker {i}", "scopes": ["beta"]}
        for i in range(30)
    ])
    after = [(fact.id, score) for fact, score in search(root, scope="alpha", scorer=scorer)]
    assert after == before


@pytest.mark.parametrize("scorer", SCORERS)
def test_reused_ids_in_different_roots_do_not_share_rows_or_postings(tmp_path, scorer):
    left, right = tmp_path / "left", tmp_path / "right"
    item = add(left, "Lumen timeout is five seconds", scopes=["alpha"])
    raw = item.to_dict()
    raw.update(statement="PRIVATE_ORCHID meeting occurs Tuesday", scopes=["beta"])
    file_path(right).parent.mkdir(parents=True)
    file_path(right).write_text(json.dumps(raw) + "\n", encoding="utf-8")
    assert search(left, scorer=scorer)[0][0].statement == item.statement
    assert search(right, "Lumen timeout", scorer=scorer) == []
    assert search(right, "PRIVATE_ORCHID", scope="alpha", scorer=scorer) == []
    assert search(right, "PRIVATE_ORCHID", scope="beta", scorer=scorer)[0][0].id == item.id
    assert "PRIVATE_ORCHID" not in str(search(left, scorer=scorer))


def test_returned_rows_and_nested_fields_cannot_mutate_a_cached_snapshot(tmp_path):
    from commontrace import fact_index

    premise = add(tmp_path, "Instrument recorded the Lumen timeout", scopes=["alpha"])
    receipt = bind_evidence(str(tmp_path), "fact", premise.id)
    target = add(tmp_path, "Lumen timeout is five seconds", scopes=["alpha"], evidence=[receipt],
                 source_trace_id="trace-original")
    view = fact_index.snapshot_facts(str(tmp_path))
    original = view[target.id].to_dict()
    returned = hierarchical.load_facts(str(tmp_path))[target.id]
    returned.statement = "PRIVATE_FORGED_CONTENT"
    returned.scopes.append("beta")
    returned.source_traces.append("forged-source")
    returned.evidence.clear()
    returned.forgotten = True
    ranked = search(tmp_path, scope="alpha")[0][0]
    ranked.scopes.clear()
    ranked.source_traces.clear()
    assert fact_index.snapshot_facts(str(tmp_path))[target.id].to_dict() == original
    assert view[target.id].to_dict() == original
    assert hierarchical.load_facts(str(tmp_path))[target.id].to_dict() == original


def test_held_views_own_mutable_copies_but_reject_access_after_source_changes(tmp_path):
    from commontrace import fact_index

    fact = add(tmp_path, "Lumen timeout is five seconds", scopes=["alpha"])
    first = fact_index.snapshot_facts(str(tmp_path))
    second = fact_index.snapshot_facts(str(tmp_path))
    owned = first[fact.id]
    owned.statement = "Edited request-local copy"
    owned.scopes.append("beta")
    assert first[fact.id] is owned
    assert second[fact.id].to_dict() == fact.to_dict()
    assert fact_index.snapshot_facts(str(tmp_path))[fact.id].to_dict() == fact.to_dict()
    rewrite_in_another_process(file_path(tmp_path), "timeout is five", "timeout is nine", atomic=True)
    for held in (first, second):
        with pytest.raises(fact_index.FactSnapshotChanged):
            held[fact.id]
    # Caller-owned objects are intentionally retained; the cache promises
    # current admission on access rather than physical erasure of references.
    assert owned.statement == "Edited request-local copy"
    assert search(tmp_path)[0][0].statement == "Lumen timeout is nine seconds"


def test_statistics_registration_race_cannot_publish_an_orphan_old_generation(tmp_path, monkeypatch):
    from commontrace import fact_index

    add(tmp_path, "Lumen timeout is five seconds")
    fact_index.clear_cache()
    original = fact_index._STATISTICS.get_or_load
    changed = False

    def change_before_flight(key, loader):
        nonlocal changed
        if not changed:
            changed = True
            # Registration has happened but no cache flight exists yet.
            # A second actual reader invalidates that generation before the
            # first reader starts constructing its statistics flight.
            rewrite_in_another_process(file_path(tmp_path), "timeout is five", "timeout is nine", atomic=True)
            fact_index.snapshot_facts(str(tmp_path))
        return original(key, loader)

    monkeypatch.setattr(fact_index._STATISTICS, "get_or_load", change_before_flight)
    assert search(tmp_path, scorer="bm25-v1")[0][0].statement == "Lumen timeout is nine seconds"
    assert changed
    assert fact_index.cache_info()["statistics"]["entries"] == 1
    assert len(fact_index._STAT_KEYS) == 1


def test_oversized_category_keys_do_not_escape_statistics_retention_budget(tmp_path):
    from commontrace import fact_index

    add(tmp_path, "Lumen timeout is five seconds")
    fact_index.clear_cache()
    for suffix in ("alpha", "beta", "gamma"):
        assert search(tmp_path, category="x" * (1024 * 1024) + suffix, scorer="bm25-v1") == []
    assert fact_index.cache_info()["statistics"]["entries"] == 0
    assert not fact_index._STAT_KEYS


@pytest.mark.parametrize("atomic", [False, True])
def test_another_process_same_size_restored_mtime_write_invalidates_hot_snapshot(tmp_path, atomic):
    from commontrace import fact_index

    fact = add(tmp_path, "Lumen timeout is five seconds")
    path = file_path(tmp_path)
    before = path.stat()
    old = fact_index.snapshot_facts(str(tmp_path))
    assert search(tmp_path)[0][0].statement == fact.statement
    rewrite_in_another_process(path, "timeout is five", "timeout is nine", atomic=atomic)
    after = path.stat()
    assert before.st_mtime_ns == after.st_mtime_ns and before.st_size == after.st_size
    with pytest.raises(fact_index.FactSnapshotChanged):
        old.ensure_current()
    assert search(tmp_path)[0][0].statement == "Lumen timeout is nine seconds"


def test_retained_symlink_api_invalidates_on_retargeting(tmp_path):
    from commontrace import fact_index

    one, two, routed = tmp_path / "one", tmp_path / "two", tmp_path / "routed"
    first = add(one, "Lumen timeout is five seconds")
    second = add(two, "Lumen timeout is nine seconds")
    path = file_path(routed)
    path.parent.mkdir(parents=True)
    path.symlink_to(file_path(one))
    old = fact_index.snapshot_facts(str(routed))
    assert search(routed)[0][0].id == first.id
    temporary = path.with_suffix(".next")
    temporary.symlink_to(file_path(two))
    temporary.replace(path)
    with pytest.raises(fact_index.FactSnapshotChanged):
        old.ensure_current()
    assert search(routed)[0][0].id == second.id
    assert search(one)[0][0].id == first.id


@pytest.mark.parametrize("scorer", SCORERS)
@pytest.mark.parametrize("change", ["revoke", "body", "restore_after_revoke"])
def test_lesson_source_trust_is_current_while_fact_generation_stays_hot(tmp_path, scorer, change):
    from commontrace import fact_index

    lesson = tmp_path / "memory" / "lessons" / "lesson_lumen.md"
    lesson.parent.mkdir(parents=True)
    fm = {"name": "lesson_lumen", "status": "active", "scopes": ["alpha"]}
    body = "## Rule\nLumen timeout measurements establish a five second deadline.\n"
    fm[lesson_admission.RECEIPT_FIELD] = lesson_admission.issue(str(tmp_path), str(lesson), fm, body,
                                                              actor="reviewer")
    frontmatter.write(str(lesson), fm, body)
    target = add(tmp_path, "Lumen timeout is five seconds", scopes=["alpha"],
                 evidence=[bind_evidence(str(tmp_path), "lesson", "lumen")])
    fallback = add(tmp_path, "Lumen timeout troubleshooting has more detailed fallback instructions", scopes=["alpha"])
    before = fact_index.snapshot_facts(str(tmp_path)).generation
    assert search(tmp_path, scope="alpha", scorer=scorer, limit=1)[0][0].id == target.id
    if change == "body":
        frontmatter.write(str(lesson), fm, body + "Unreviewed ordinary instruction.\n")
    else:
        lesson_admission.revoke(str(tmp_path), str(lesson), actor="reviewer")
        if change == "restore_after_revoke":
            frontmatter.write(str(lesson), fm, body)
    assert fact_index.snapshot_facts(str(tmp_path)).generation == before
    assert search(tmp_path, scope="alpha", scorer=scorer, limit=1)[0][0].id == fallback.id
    assert target.id not in {fact.id for fact, _ in search(tmp_path, scope="alpha", scorer=scorer,
                                                        as_of="2026-05-01T00:00:00Z")}


@pytest.mark.parametrize("scorer", SCORERS)
def test_approval_policy_change_invalidates_legacy_dependencies_without_fact_file_change(tmp_path, scorer):
    from commontrace import fact_index

    lesson = tmp_path / "memory" / "lessons" / "lesson_lumen.md"
    lesson.parent.mkdir(parents=True)
    fm = {"name": "lesson_lumen", "status": "active"}
    frontmatter.write(str(lesson), fm, "Lumen timeout measurements establish a five second deadline.\n")
    target = add(tmp_path, "Lumen timeout is five seconds",
                 evidence=[bind_evidence(str(tmp_path), "lesson", "lumen")])
    fallback = add(tmp_path, "Lumen timeout troubleshooting has detailed fallback instructions")
    before = fact_index.snapshot_facts(str(tmp_path)).generation
    assert search(tmp_path, scorer=scorer, limit=1)[0][0].id == target.id
    (tmp_path / "memory" / "approval-policy.yaml").write_text("require_integrity: true\n", encoding="utf-8")
    assert fact_index.snapshot_facts(str(tmp_path)).generation == before
    assert search(tmp_path, scorer=scorer, limit=1)[0][0].id == fallback.id


@pytest.mark.parametrize("scorer", SCORERS)
@pytest.mark.parametrize("change", ["forget", "delete", "correct"])
def test_current_fact_source_mutations_invalidate_governed_targets(tmp_path, scorer, change):
    premise = add(tmp_path, "The instrument recorded a five second deadline")
    target = add(tmp_path, "Lumen timeout is five seconds",
                 evidence=[bind_evidence(str(tmp_path), "fact", premise.id)])
    fallback = add(tmp_path, "Lumen timeout troubleshooting has detailed fallback instructions")
    assert search(tmp_path, scorer=scorer, limit=1)[0][0].id == target.id
    if change == "forget":
        hierarchical.forget_fact(str(tmp_path), premise.id)
    elif change == "delete":
        hierarchical.delete_fact(str(tmp_path), premise.id)
    else:
        hierarchical.update_fact(str(tmp_path), premise.id, statement="The instrument recorded a nine second deadline")
    assert search(tmp_path, scorer=scorer, limit=1)[0][0].id == fallback.id
    assert target.id not in {fact.id for fact, _ in search(tmp_path, scorer=scorer, as_of="2026-05-01T00:00:00Z")}


@pytest.mark.parametrize("scorer", SCORERS)
def test_invalid_high_scoring_candidates_are_refilled_from_live_lower_ranks(tmp_path, scorer):
    for i in range(15):
        add(tmp_path, "Lumen timeout " + "x" * (i + 1), evidence=[], confidence=1.0)
    live = add(tmp_path, "Lumen timeout troubleshooting has detailed recovery instructions", confidence=0.5)
    assert [fact.id for fact, _ in search(tmp_path, scorer=scorer, limit=1)] == [live.id]


def test_unrelated_postings_do_not_trigger_evidence_assessment(tmp_path, monkeypatch):
    premise = add(tmp_path, "Instrument recorded an approved deadline")
    receipt = bind_evidence(str(tmp_path), "fact", premise.id)
    hierarchical.add_facts(str(tmp_path), [
        {"statement": f"Orchid private background marker {i}", "evidence": [receipt]}
        for i in range(80)
    ])
    target = add(tmp_path, "Lumen timeout is five seconds", evidence=[receipt])
    visited = []
    assess = EvidenceResolver.assess

    def record(self, identity):
        visited.append(identity)
        return assess(self, identity)

    monkeypatch.setattr(EvidenceResolver, "assess", record)
    assert [fact.id for fact, _ in search(tmp_path)] == [target.id]
    assert visited == [target.id]


@pytest.mark.parametrize("scorer", [None, False, 0, [], {}, "", "BM25", "bm25-v1\n"])
def test_explicit_invalid_scorer_never_coalesces_to_a_default(tmp_path, scorer):
    add(tmp_path, "Lumen timeout is five seconds")
    with pytest.raises(ValueError):
        search(tmp_path, scorer=scorer)


@pytest.mark.parametrize("scorer", SCORERS)
def test_candidate_materialization_during_crossprocess_replacement_retries_the_whole_snapshot(
    tmp_path, monkeypatch, scorer,
):
    from commontrace import fact_index

    add(tmp_path, "Lumen timeout is five seconds")
    add(tmp_path, "Orchid timeout is five seconds")
    copy = fact_index._Record.copy
    changed = False

    def interrupted_copy(record):
        nonlocal changed
        fact = copy(record)
        if not changed:
            changed = True
            rewrite_in_another_process(file_path(tmp_path), "timeout is five", "timeout is nine", atomic=True)
        return fact

    monkeypatch.setattr(fact_index._Record, "copy", interrupted_copy)
    result = search(tmp_path, "timeout", scorer=scorer, limit=2)
    assert changed and len(result) == 2
    assert all("nine" in fact.statement and "five" not in fact.statement for fact, _ in result)


def test_source_replacement_during_cold_read_cannot_publish_the_old_payload(tmp_path, monkeypatch):
    from commontrace import _jsonl, fact_index

    add(tmp_path, "Lumen timeout is five seconds")
    reader = _jsonl.read_rows
    changed = False

    def interrupted_read(path):
        nonlocal changed
        rows = reader(path)
        if Path(path) == file_path(tmp_path) and not changed:
            changed = True
            rewrite_in_another_process(file_path(tmp_path), "timeout is five", "timeout is nine", atomic=True)
        return rows

    monkeypatch.setattr(_jsonl, "read_rows", interrupted_read)
    view = fact_index.snapshot_facts(str(tmp_path))
    assert changed and all("nine" in fact.statement for fact in view.values())
    assert search(tmp_path)[0][0].statement == "Lumen timeout is nine seconds"


def test_missing_generation_is_invalidated_on_creation_and_deletion(tmp_path):
    from commontrace import fact_index

    absent = fact_index.snapshot_facts(str(tmp_path))
    assert len(absent) == 0 and absent.generation is None
    fact = add(tmp_path, "Lumen timeout is five seconds")
    with pytest.raises(fact_index.FactSnapshotChanged):
        absent.ensure_current()
    present = fact_index.snapshot_facts(str(tmp_path))
    assert present[fact.id].statement == fact.statement
    file_path(tmp_path).unlink()
    with pytest.raises(fact_index.FactSnapshotChanged):
        present.ensure_current()
    assert search(tmp_path) == []


@pytest.mark.parametrize("scorer", [None, False, 0, [], {}, "", "BM25", "bm25-v1\n"])
def test_public_recall_and_explorer_strictly_validate_explicit_scorer(tmp_path, scorer):
    add(tmp_path, "Lumen timeout is five seconds")
    with pytest.raises(ValueError):
        recall.recall(str(tmp_path), "Lumen timeout", channels=("facts",), fact_scorer=scorer)
    with pytest.raises(explorer.ExplorerError):
        explorer.inspect(str(tmp_path), {"question": "Lumen timeout", "fact_scorer": scorer})
    service = gateway.Gateway(str(tmp_path), token="test-scorer-authorization-token-123456789")
    result = service.handle("POST", "/v1/explore",
                            {"Authorization": "Bearer test-scorer-authorization-token-123456789"},
                            json.dumps({"question": "Lumen timeout", "fact_scorer": scorer}).encode())
    assert result.status == 400
    response = json.loads(result.body)
    assert response["error"]["code"] == "bad_request" and "items" not in response


@pytest.mark.parametrize("scorer", ["", "BM25", "bm25-v1\n"])
def test_actual_mcp_envelopes_reject_unknown_scorer(tmp_path, scorer):
    pytest.importorskip("mcp")
    from commontrace import mcp_server

    add(tmp_path, "Lumen timeout is five seconds")
    server = mcp_server.build_server(str(tmp_path))
    for name, field in (("memory_recall", "fact_scorer"), ("query_facts", "scorer")):
        arguments = {"question": "Lumen timeout", "channels": ["facts"]} if name == "memory_recall" \
            else {"query": "Lumen timeout"}
        arguments[field] = scorer
        result = asyncio.run(server.call_tool(name, arguments))
        structured = getattr(result, "structured_content", None)
        payload = structured.get("result", structured) if structured else json.loads(result.content[0].text)
        assert payload["ok"] is False
        assert "facts" not in payload and "items" not in payload


@pytest.mark.parametrize("scorer", [None, False, 0, [], {}])
def test_actual_mcp_schema_rejects_non_string_scorer(tmp_path, scorer):
    pytest.importorskip("mcp")
    try:
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:
        from mcp.server.fastmcp.exceptions import ToolError

    from commontrace import mcp_server

    server = mcp_server.build_server(str(tmp_path))
    for name, field in (("memory_recall", "fact_scorer"), ("query_facts", "scorer")):
        arguments = {"question": "Lumen timeout", "channels": ["facts"]} if name == "memory_recall" \
            else {"query": "Lumen timeout"}
        arguments[field] = scorer
        with pytest.raises(ToolError, match="validation"):
            asyncio.run(server.call_tool(name, arguments))


@pytest.mark.parametrize("question,evidence", [
    ("électricité", "électromagnétisme est documenté"),
    ("batería", "bateristas han llegado"),
    ("distribución", "distribution is documented"),
])
def test_non_ascii_subjects_do_not_acquire_unrelated_prefix_matches(question, evidence):
    coverage = assess(question, [evidence])
    assert coverage.abstain and coverage.matched_terms == 0
    assert coverage.query_terms > 0 and coverage.missing_subject_terms


def test_multilingual_partial_subject_coverage_stays_explicit_and_uncalibrated():
    coverage = assess("astronomía presupuesto", ["Una observación de astronomía."])
    assert coverage.confidence == 0.5
    assert coverage.matched_terms == 1 and coverage.query_terms == 2
    assert coverage.missing_subject_terms == ("presupuesto",)
    assert "unverified" in coverage.reason


@pytest.mark.parametrize("question,evidence", [
    ("What is Élise bicycle serial number?", "Élise bought a bicycle."),
    ("Nia 自転車 serial number", "Niaは青い自転車を買った。"),
])
def test_identifier_still_requires_explicit_recorded_evidence_across_languages(question, evidence):
    absent = assess(question, [evidence], labels=["serial"])
    assert absent.abstain and absent.confidence == 0
    recorded = assess(question, [evidence + " serial number CT-492"])
    assert not recorded.abstain and recorded.matched_terms > absent.matched_terms


def test_actual_cjk_recall_reports_lexical_coverage_instead_of_empty_ascii_terms(tmp_path):
    fact = add(tmp_path, "灯塔数据库超时为五秒")
    result = recall.recall(str(tmp_path), "灯塔数据库超时", channels=("facts",), fact_scorer="bm25-v1")
    assert [item.id for item in result.items] == [f"fact:{fact.id}"]
    assert result.assessment.query_terms > 0 and result.assessment.matched_query_terms > 0
    assert not result.assessment.abstain and result.assessment.confidence > 0
    assert "correctness" not in result.assessment.reason or "unverified" in result.assessment.reason
