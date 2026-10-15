"""Actual bounded source reading and scoped recall, without a generated answer."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from commontrace import frontmatter, graph, hierarchical, lesson_admission, recall
from commontrace.conversation import Store
from commontrace.evidence_context import explain_fact
from commontrace.fact_evidence import EvidenceError, EvidenceResolver, FactEvidence, bind_evidence, claim_revision


@pytest.fixture(params=["jsonl", "sqlite"])
def root(tmp_path: Path, request) -> str:
    if request.param == "sqlite":
        from commontrace import fact_store
        fact_store.migrate(str(tmp_path))
    return str(tmp_path)


def add(root, statement, *, sources=None, scopes=None, refute=False):
    receipts = None if sources is None else [bind_evidence(root, "fact", fact.id,
                                                         polarity="refute" if refute else "support")
                                           for fact in sources]
    return hierarchical.add_fact(root, statement, evidence=receipts, scopes=scopes,
                                 valid_from="2026-01-01T00:00:00Z")[0]


def cli(root, *arguments):
    return subprocess.run([sys.executable, "-m", "commontrace.cli", *arguments, "--dest", root],
                          text=True, capture_output=True, check=False)


def test_linked_premises_are_quoted_once_and_all_paths_are_preserved(root):
    leaf = add(root, "Instrument L recorded a timeout of 5 seconds")
    first = add(root, "Instrument L is the reference measurement", sources=[leaf])
    second = add(root, "Instrument L supplied the operating constraint", sources=[leaf])
    target = add(root, "The Lumen database timeout is 5 seconds", sources=[first, second])
    proof = explain_fact(root, target.id)
    assert proof.assessment.eligible
    assert len(proof.nodes) == 3
    assert len(proof.edges) == 4
    assert proof.context.count(leaf.statement) == 1
    assert {node.quote for node in proof.nodes} == {leaf.statement, first.statement, second.statement}
    leaf_node = next(node for node in proof.nodes if node.source_id == leaf.id)
    assert leaf_node.depth == 2
    assert leaf_node.valid_from == leaf.valid_from
    assert proof.claim_revision == claim_revision(target)
    assert all(edge.recorded_at > leaf.valid_from for edge in proof.edges)
    assert proof.tokens <= proof.budget


def test_refutation_and_support_are_visible_without_approving_withheld_claim(root):
    premise = add(root, "The instrument reports conflicting Lumen timeout measurements")
    target = add(root, "The Lumen timeout is 5 seconds", sources=[premise])
    target, _ = hierarchical.add_fact(root, target.statement,
                                      evidence=[bind_evidence(root, "fact", premise.id, polarity="refute")])
    proof = explain_fact(root, target.id)
    assert proof.assessment.status == "refuted"
    assert len(proof.nodes) == 1
    assert {edge.polarity for edge in proof.edges} == {"support", "refute"}
    assert "refute/support" in proof.context
    packed = recall.recall(root, target.statement, channels=("facts",), evidence_budget=512)
    assert all(item.id != "fact:" + target.id for item in packed.items)


@pytest.mark.parametrize("body", ["漢字🙂" * 1000, 'A quote " and a newline\n' * 500])
def test_quote_clipping_preserves_json_data_and_the_token_bound(root, body):
    premise = add(root, body[:1900])
    target = add(root, "The measured Lumen timeout is constrained", sources=[premise])
    proof = explain_fact(root, target.id, budget=64)
    assert proof.tokens <= 64
    assert proof.nodes[0].truncated
    assert premise.statement.startswith(proof.nodes[0].quote)
    assert "[quote truncated]" in proof.context
    assert "source quote truncated to the evidence token budget" in proof.omissions
    quoted = proof.context.split("] ", 1)[1].removesuffix(" [quote truncated]")
    assert json.loads(quoted) == proof.nodes[0].quote


def test_zero_budget_exposes_assessment_without_fabricating_quotes(root):
    premise = add(root, "Instrument logged the Lumen timeout")
    target = add(root, "The measured timeout is constrained", sources=[premise])
    proof = explain_fact(root, target.id, budget=0)
    assert proof.assessment.eligible
    assert proof.nodes == proof.edges == ()
    assert proof.context == ""
    assert proof.tokens == 0
    assert "evidence token budget reached" in proof.omissions


def test_depth_and_source_limits_are_explicit(root):
    leaf = add(root, "Instrument logged the Lumen timeout")
    middle = add(root, "Instrument corroboration is available", sources=[leaf])
    other = add(root, "A second instrument recorded the timeout")
    target = add(root, "The timeout constraint is documented", sources=[middle, other])
    depth = explain_fact(root, target.id, max_depth=1)
    assert {node.source_id for node in depth.nodes} == {middle.id, other.id}
    assert "source depth limit reached" in depth.omissions
    count = explain_fact(root, target.id, max_sources=1)
    assert len(count.nodes) == 1
    assert "source count limit reached" in count.omissions


@pytest.mark.parametrize("options", [{"budget": -1}, {"budget": True}, {"budget": 8193},
                                      {"max_sources": 0}, {"max_sources": 65}, {"max_depth": 0},
                                      {"max_depth": 17}, {"scope": "bad\nheader"}])
def test_expansion_limits_are_strict(root, options):
    target = add(root, "Legacy Lumen fact")
    with pytest.raises(ValueError):
        explain_fact(root, target.id, **options)


def test_legacy_claim_is_explained_without_invented_provenance(root):
    target = add(root, "Legacy Lumen fact")
    proof = explain_fact(root, target.id)
    assert proof.assessment.status == "unbound"
    assert proof.nodes == ()
    assert "legacy claim has no revision-bound evidence graph" in proof.omissions


def test_unknown_forgotten_and_deleted_targets_are_withheld(root):
    target = add(root, "A forgotten Lumen fact")
    hierarchical.forget_fact(root, target.id)
    for identity in ("missing", target.id):
        with pytest.raises(EvidenceError):
            explain_fact(root, identity)
    hierarchical.forget_fact(root, target.id, undo=True)
    hierarchical.delete_fact(root, target.id)
    with pytest.raises(EvidenceError):
        explain_fact(root, target.id, as_of="2026-05-01T00:00:00Z")


def test_current_source_revocation_and_correction_remove_quotes(root):
    premise = add(root, "A measured timeout of 5 seconds")
    target = add(root, "The Lumen timeout is 5 seconds", sources=[premise])
    hierarchical.update_fact(root, premise.id, statement="A measured timeout of 9 seconds")
    proof = explain_fact(root, target.id)
    assert proof.assessment.status == "stale"
    assert proof.nodes == ()
    assert premise.statement not in proof.context
    hierarchical.add_fact(root, target.statement, evidence=[bind_evidence(root, "fact", premise.id)])
    fresh = explain_fact(root, target.id)
    assert fresh.assessment.eligible
    assert "9 seconds" in fresh.context
    hierarchical.delete_fact(root, premise.id)
    historical = explain_fact(root, target.id, as_of="2026-05-01T00:00:00Z")
    assert not historical.assessment.eligible
    assert historical.nodes == ()


def test_out_of_scope_target_and_corrupted_dependency_do_not_expose_private_bodies(root):
    private = add(root, "PRIVATE_FINANCE_TIMEOUT", scopes=["finance"])
    allowed = add(root, "A payments timeout claim", scopes=["payments"], sources=[])
    with hierarchical.mutate_facts(root) as facts:
        fact = facts[allowed.id]
        fact.evidence = [bind_evidence(root, "fact", private.id)]
        fact.evidence_revision = claim_revision(fact)
    proof = explain_fact(root, allowed.id, scope="payments")
    assert not proof.assessment.eligible
    assert proof.nodes == proof.edges == ()
    assert "PRIVATE_FINANCE" not in json.dumps(proof.to_dict())
    assert private.id not in json.dumps(proof.to_dict())
    with pytest.raises(EvidenceError):
        explain_fact(root, private.id, scope="payments")


def test_lesson_change_between_assessment_and_quote_assembly_is_withheld(root, monkeypatch):
    path = Path(root, "memory", "lessons", "lesson_lumen.md")
    path.parent.mkdir(parents=True)
    fm = {"name": "lesson_lumen", "status": "active"}
    frontmatter.write(str(path), fm, "Original recorded measurement\n")
    receipt = bind_evidence(root, "lesson", "lesson_lumen")
    target, _ = hierarchical.add_fact(root, "Lumen timeout constraint", evidence=[receipt])
    original = EvidenceResolver.source_quote
    def changed(self, evidence):
        frontmatter.write(str(path), fm, "Corrected recorded measurement\n")
        return original(self, evidence)
    monkeypatch.setattr(EvidenceResolver, "source_quote", changed)
    proof = explain_fact(root, target.id)
    assert proof.nodes == ()
    assert not proof.assessment.eligible
    assert "source changed while its quote was read" in proof.omissions


def test_external_unsigned_lesson_symlink_is_neither_bound_nor_read_as_evidence(root, monkeypatch):
    external = Path(root).parent / (Path(root).name + "-external.md")
    body = "EXTERNAL_PRIVATE_SOURCE_BYTES\n"
    fm = {"name": "lesson_external", "status": "active"}
    frontmatter.write(str(external), fm, body)
    link = Path(root, "memory", "lessons", "lesson_external.md")
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    receipt = FactEvidence("lesson", "external", lesson_admission.digest_of(fm, body))
    reads = []
    original = frontmatter.read
    def counted(path):
        reads.append(path)
        return original(path)
    monkeypatch.setattr(frontmatter, "read", counted)
    with pytest.raises(EvidenceError):
        bind_evidence(root, "lesson", "lesson_external")
    assert EvidenceResolver(root, {}).source_quote(receipt) is None
    assert reads == []


def test_scoped_recall_excludes_other_fact_and_lesson_domains(root):
    public = add(root, "The shared Lumen timeout is 5 seconds")
    ours = add(root, "The payments Lumen timeout is 6 seconds", scopes=["payments"])
    theirs = add(root, "The finance Lumen timeout is PRIVATE_FINANCE", scopes=["finance"])
    for suffix, scope in (("payments", "payments"), ("finance", "finance")):
        path = Path(root, "memory", "lessons", f"lesson_{suffix}.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        frontmatter.write(str(path), {"name": f"lesson_{suffix}", "status": "active", "scopes": [scope],
                                     "description": f"Lumen timeout {suffix}", "tags": ["lumen", "timeout"]},
                          f"## Rule\nLumen timeout belongs to {suffix}.\n")
    packed = recall.recall(root, "Lumen timeout", channels=("lessons", "facts"), scope="payments")
    ids = {item.id for item in packed.items}
    assert {"fact:" + public.id, "fact:" + ours.id} <= ids
    assert "fact:" + theirs.id not in ids
    assert "finance" not in packed.context
    assert "payments" in packed.context


def test_scoped_graph_and_conversation_enumeration_fail_closed(root):
    graph.add_node(root, "service:lumen", "service", "Lumen")
    graph.add_edge(root, "service:lumen", "place:private", "located_in", valid_at="2026-01-01")
    with Store(root, "finance") as store:
        store.add("s1", [{"speaker": "ana", "text": "Lumen is PRIVATE_FINANCE"}])
    scoped = recall.recall(root, "Lumen", scope="payments", channels=("graph", "conversations"))
    assert scoped.items == []
    with Store(root, "container-payments-chat") as store:
        store.add("s1", [{"speaker": "ana", "text": "Lumen serves payments"}])
    explicit = recall.recall(root, "Lumen", scope="container:payments", channels=("conversations",),
                             spaces=["container-payments-chat"])
    assert "payments" in explicit.context
    assert "PRIVATE_FINANCE" not in explicit.context


def test_fact_expansion_is_opt_in_and_packed_within_total_budget(root):
    premise = add(root, "An instrument measured 5 seconds after a pool reset")
    target = add(root, "The Lumen timeout is 5 seconds", sources=[premise])
    default = recall.recall(root, "Lumen timeout", channels=("facts",))
    assert "Evidence quotations" not in default.context
    expanded = recall.recall(root, "Lumen timeout", channels=("facts",), evidence_budget=128, budget=300)
    assert expanded.tokens <= 300
    item = next(item for item in expanded.items if item.id == "fact:" + target.id)
    assert premise.statement in item.text
    proof = item.provenance["evidence_context"]
    assert proof["nodes"][0]["quote"] == premise.statement
    assert proof["tokens"] <= 128


def test_evidence_allowance_counts_all_joined_quote_text_once(root):
    for index in range(4):
        premise = add(root, f"Instrument number {index} measured the timeout independently")
        add(root, f"Lumen timeout constraint {index}", sources=[premise])
    expanded = recall._facts(root, "Lumen timeout", None, 12, evidence_budget=128)
    added_tokens = 0
    for item in expanded:
        proof = item.provenance.get("evidence_context")
        if proof is not None:
            added_tokens += recall.tokens(item.text) - recall.tokens(proof["statement"])
    assert 0 < added_tokens <= 128


def test_expanding_many_candidates_reuses_a_bounded_number_of_fact_snapshots(root, monkeypatch):
    for index in range(8):
        premise = add(root, f"Instrument number {index} measured 5 seconds")
        add(root, f"Lumen timeout constraint {index}", sources=[premise])
    original = hierarchical.load_facts
    reads = []
    def counted(store_root):
        reads.append(store_root)
        return original(store_root)
    monkeypatch.setattr(hierarchical, "load_facts", counted)
    expanded = recall.recall(root, "Lumen timeout", channels=("facts",), evidence_budget=4096)
    assert len([item for item in expanded.items if item.provenance.get("evidence_context")]) >= 4
    assert len(reads) <= 3


def test_token_truncation_does_not_leave_unconnected_descendant_quotes(root):
    leaf = add(root, "An underlying instrument measurement")
    parent = add(root, "The instrument corroborates the constraint", sources=[leaf])
    with hierarchical.mutate_facts(root) as facts:
        moved = facts.pop(parent.id)
        moved.id = "p" * 190
        facts[moved.id] = moved
    target = add(root, "Lumen timeout constraint", sources=[moved])
    proof = explain_fact(root, target.id, budget=64)
    assert proof.nodes == proof.edges == ()
    assert "source path omitted with its parent quote" in proof.omissions


def test_lesson_provenance_distinguishes_legacy_compatibility_from_bound_approval(root):
    path = Path(root, "memory", "lessons", "lesson_lumen.md")
    path.parent.mkdir(parents=True)
    fm = {"name": "lesson_lumen", "status": "active", "description": "Lumen timeout constraint",
          "tags": ["lumen", "timeout"], "scopes": ["payments"], "source_traces": ["trace-measured"]}
    frontmatter.write(str(path), fm, "## Rule\nUse the measured Lumen timeout.\n")
    legacy = recall.recall(root, "Lumen timeout", channels=("lessons",), scope="payments")
    provenance = legacy.items[0].provenance
    assert provenance["admission"] == "legacy_compatible"
    assert provenance["source_traces"] == ["trace-measured"]
    assert provenance["scopes"] == ["payments"]
    fm, body = frontmatter.read(str(path))
    fm[lesson_admission.RECEIPT_FIELD] = lesson_admission.issue(root, str(path), fm, body, actor="reviewer")
    frontmatter.write(str(path), fm, body)
    approved = recall.recall(root, "Lumen timeout", channels=("lessons",), scope="payments")
    assert approved.items[0].provenance["admission"] == "verified"


def test_scope_change_after_ranking_does_not_leak_a_formerly_public_fact(root, monkeypatch):
    target = add(root, "Lumen secret that is moved to finance")
    original = hierarchical.search_facts
    def ranked_then_private(*args, **kwargs):
        ranked = original(*args, **kwargs)
        with hierarchical.mutate_facts(root) as facts:
            facts[target.id].scopes = ["finance"]
        return ranked
    monkeypatch.setattr(hierarchical, "search_facts", ranked_then_private)
    assert recall.recall(root, "Lumen", channels=("facts",), scope="payments").items == []


@pytest.mark.parametrize("evidence_budget", [0, 512])
def test_lesson_revocation_after_ranking_withholds_fact_even_with_unchanged_fact_generation(
    root, monkeypatch, evidence_budget,
):
    from commontrace import fact_index

    path = Path(root, "memory", "lessons", "lesson_lumen.md")
    path.parent.mkdir(parents=True)
    fm = {"name": "lesson_lumen", "status": "active"}
    body = "The approved instrument measured a five second Lumen timeout.\n"
    fm[lesson_admission.RECEIPT_FIELD] = lesson_admission.issue(root, str(path), fm, body, actor="reviewer")
    frontmatter.write(str(path), fm, body)
    target, _ = hierarchical.add_fact(root, "Lumen timeout is five seconds",
                                      evidence=[bind_evidence(root, "lesson", "lesson_lumen")])
    generation = fact_index.snapshot_facts(root).generation
    original = hierarchical.search_facts

    def ranked_then_revoked(*args, **kwargs):
        ranked = original(*args, **kwargs)
        assert target.id in {fact.id for fact, _ in ranked}
        lesson_admission.revoke(root, str(path), actor="reviewer")
        assert fact_index.snapshot_facts(root).generation == generation
        return ranked

    monkeypatch.setattr(hierarchical, "search_facts", ranked_then_revoked)
    result = recall.recall(root, "Lumen timeout", channels=("facts",), evidence_budget=evidence_budget)
    assert not result.items and "five seconds" not in result.context


def test_cli_explanation_and_expanded_recall_use_production_sources(root):
    assert cli(root, "init", "--agent-type", "coding").returncode == 0
    premise = add(root, "Instrument logged a 5 second Lumen timeout")
    target = add(root, "The Lumen timeout is 5 seconds", sources=[premise])
    result = cli(root, "fact", "explain", target.id, "--budget", "128", "--json")
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["nodes"][0]["quote"] == premise.statement
    expanded = cli(root, "recall", "Lumen timeout", "--channel", "facts", "--evidence-budget", "128", "--json")
    assert expanded.returncode == 0, expanded.stderr
    assert premise.statement in json.loads(expanded.stdout)["context"]


def test_real_mcp_explain_and_memory_recall_transport_new_options(root):
    pytest.importorskip("mcp")
    from commontrace import mcp_server
    assert cli(root, "init", "--agent-type", "coding").returncode == 0
    premise = add(root, "The instrument logged the timeout as 5 seconds", scopes=["payments"])
    target = add(root, "Lumen timeout is 5 seconds", sources=[premise], scopes=["payments"])
    add(root, "Lumen timeout PRIVATE_FINANCE", scopes=["finance"])
    server = mcp_server.build_server(root)
    def call(name, **arguments):
        result = asyncio.run(server.call_tool(name, arguments))
        if getattr(result, "structured_content", None):
            return result.structured_content.get("result", result.structured_content)
        return json.loads(result.content[0].text)
    proof = call("fact_explain", fact_id=target.id, scope="payments")
    assert proof["ok"], proof
    assert proof["nodes"][0]["quote"] == premise.statement
    assert not call("fact_explain", fact_id=target.id, scope="finance")["ok"]
    packed = call("memory_recall", question="Lumen timeout", channels=["facts"],
                  evidence_budget=128, scope="payments")
    assert packed["ok"], packed
    assert premise.statement in packed["context"]
    assert "PRIVATE_FINANCE" not in packed["context"]
