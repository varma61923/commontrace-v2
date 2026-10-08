"""Scorer contracts through actual CLI, SDK tools and authenticated HTTP.

Fixtures are canonical production writes. Invalid high-ranking evidence must
not occupy the requested result limit, and changing a scorer never changes
which tenant or source revisions an agent may read.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

from commontrace import gateway, hierarchical
from commontrace.fact_evidence import bind_evidence

SCORERS = ("overlap-v1", "bm25-v1")
QUERY = "Aster gateway timeout"
SCOPE = "container:alpha"
TOKEN = "fact-interface-synthetic-credential"


@dataclass(frozen=True)
class Corpus:
    root: str
    source: hierarchical.AtomicFact
    target: hierarchical.AtomicFact
    unsupported: hierarchical.AtomicFact
    private: hierarchical.AtomicFact
    expired: hierarchical.AtomicFact


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    root = str(tmp_path)
    source, _ = hierarchical.add_fact(root, "Alpha observed the Aster service timer in an instrument log.",
                                      scopes=[SCOPE], valid_from="2026-01-01T00:00:00Z")
    receipt = bind_evidence(root, "fact", source.id)
    target, _ = hierarchical.add_fact(root, "Aster gateway timeout uses the verified scheduling configuration.",
                                      scopes=[SCOPE], evidence=[receipt], valid_from="2026-01-01T00:00:00Z")
    unsupported, _ = hierarchical.add_fact(root, "Aster gateway timeout.", scopes=[SCOPE],
                                           confidence=1.0, evidence=[receipt], min_support=2)
    private, _ = hierarchical.add_fact(root, "SECRET_BETA Aster gateway timeout needs a different account.",
                                       scopes=["container:beta"], confidence=1.0)
    expired, _ = hierarchical.add_fact(root, "EXPIRED_ALPHA Aster gateway timeout historical entry.",
                                       scopes=[SCOPE], expires_at="2020-01-01T00:00:00Z")
    hierarchical.add_fact(root, "The Aster timeout investigation requires corroborated diagnostic evidence.")
    return Corpus(root, source, target, unsupported, private, expired)


@pytest.fixture
def endpoint(corpus: Corpus) -> Iterator[str]:
    app = gateway.Gateway(corpus.root, token=TOKEN, durable=False)
    server = gateway.make_http_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def cli(corpus: Corpus, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "commontrace.cli", *arguments, "--dest", corpus.root],
                          capture_output=True, text=True, check=False, timeout=30)


def http(endpoint: str, body: dict[str, Any], *, authenticated: bool = True) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json", "X-Container-Tag": "alpha"}
    if authenticated:
        headers["Authorization"] = "Bearer " + TOKEN
    request = urllib.request.Request(endpoint + "/v1/explore", data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def tool(server: Any, name: str, **arguments: Any) -> dict[str, Any]:
    """Use FastMCP's public SDK validation/dispatch rather than private callables."""
    response = asyncio.run(server.call_tool(name, arguments))
    structured = getattr(response, "structured_content", None)
    if structured:
        return structured.get("result", structured)
    return json.loads(response.content[0].text)


def assert_admitted(corpus: Corpus, payload: dict[str, Any], scorer: str) -> None:
    assert payload["fact_scorer"] == scorer
    assert not payload["errors"]
    ids = {item["id"] for item in payload["items"]}
    assert "fact:" + corpus.target.id in ids
    assert not ids & {"fact:" + fact.id for fact in (corpus.unsupported, corpus.private, corpus.expired)}
    assert "SECRET_BETA" not in json.dumps(payload) and "EXPIRED_ALPHA" not in json.dumps(payload)
    target = next(item for item in payload["items"] if item["id"] == "fact:" + corpus.target.id)
    # BM25 reports stemmed index terms; the legacy overlap profile preserves
    # literal terms. These fixed expectations do not call the scorer itself.
    expected_terms = ["aster", "gatewai", "timeout"] if scorer == "bm25-v1" else ["aster", "gateway", "timeout"]
    assert target["provenance"]["search"] == {"scorer": scorer, "matched_terms": expected_terms}
    assert any(node["quote"] == corpus.source.statement for node in target["provenance"]["evidence_context"]["nodes"])


def test_default_overlap_cli_remains_explicitly_identical_with_legacy_score(corpus: Corpus) -> None:
    arguments = ("fact", "search", QUERY, "--scope", SCOPE, "--limit", "1")
    default = cli(corpus, *arguments)
    explicit = cli(corpus, *arguments, "--scorer", "overlap-v1")
    assert default.returncode == explicit.returncode == 0
    assert default.stdout == explicit.stdout and default.stderr == explicit.stderr
    row = next(line.split(maxsplit=3) for line in default.stdout.splitlines() if corpus.target.id in line)
    # The retained overlap profile uses the historical Jaccard/confidence blend.
    assert float(row[0]) == pytest.approx(0.5025, abs=0.00051)
    assert corpus.unsupported.id not in default.stdout
    packed = cli(corpus, "recall", QUERY, "--channel", "facts", "--scope", SCOPE, "--json")
    assert packed.returncode == 0
    data = json.loads(packed.stdout)
    assert data["fact_scorer"] == "overlap-v1"
    target = next(item for item in data["items"] if item["id"] == "fact:" + corpus.target.id)
    assert target["score"] == 0.5025


@pytest.mark.parametrize("scorer", SCORERS)
def test_cli_scorer_preserves_scope_quotes_and_eligibility_before_limit(corpus: Corpus, scorer: str) -> None:
    searched = cli(corpus, "fact", "search", QUERY, "--scope", SCOPE, "--limit", "1", "--scorer", scorer)
    assert searched.returncode == 0, searched.stderr
    assert corpus.target.id in searched.stdout and corpus.unsupported.id not in searched.stdout
    result = cli(corpus, "recall", QUERY, "--channel", "facts", "--scope", SCOPE,
                 "--fact-scorer", scorer, "--evidence-budget", "256", "--json")
    assert result.returncode == 0, result.stderr
    assert_admitted(corpus, json.loads(result.stdout), scorer)


@pytest.mark.parametrize("scorer", SCORERS)
def test_real_http_explorer_reports_scorer_and_cannot_override_authenticated_scope(
    corpus: Corpus, endpoint: str, scorer: str,
) -> None:
    before = hierarchical.load_facts(corpus.root)[corpus.target.id].to_dict()
    status, data = http(endpoint, {"question": QUERY, "channels": ["facts"], "fact_scorer": scorer,
                                   "scope": "container:beta", "evidence_budget": 256})
    assert status == 200 and data["scope"] == SCOPE and data["read_only"] is True
    assert_admitted(corpus, data, scorer)
    assert hierarchical.load_facts(corpus.root)[corpus.target.id].to_dict() == before
    assert not list(Path(corpus.root).rglob("holdout*.jsonl"))


@pytest.mark.parametrize("scorer", SCORERS)
def test_warm_http_reader_withdraws_erased_source_under_each_scorer(corpus: Corpus, endpoint: str, scorer: str) -> None:
    request = {"question": QUERY, "channels": ["facts"], "fact_scorer": scorer, "evidence_budget": 256}
    assert_admitted(corpus, http(endpoint, request)[1], scorer)
    assert_admitted(corpus, http(endpoint, {**request, "as_of": "2026-01-01"})[1], scorer)
    assert hierarchical.delete_fact(corpus.root, corpus.source.id)
    status, current = http(endpoint, request)
    assert status == 200
    assert "fact:" + corpus.target.id not in {item["id"] for item in current["items"]}
    assert corpus.source.statement not in current["context"]
    status, historical = http(endpoint, {**request, "as_of": "2026-01-01"})
    assert status == 200 and "fact:" + corpus.target.id not in {item["id"] for item in historical["items"]}


@pytest.mark.parametrize("value", ["unknown-v1", "", None, True, ["bm25-v1"], {"scorer": "bm25-v1"}])
def test_invalid_http_scorer_is_rejected_even_without_fact_channel(endpoint: str, value: Any) -> None:
    status, data = http(endpoint, {"question": QUERY, "channels": ["lessons"], "fact_scorer": value})
    assert status == 400 and "items" not in data
    assert data["error"]["code"] == "bad_request"


def test_authentication_precedes_invalid_scorer_validation(endpoint: str) -> None:
    status, data = http(endpoint, {"question": QUERY, "fact_scorer": "unknown-v1"}, authenticated=False)
    assert status == 401 and "items" not in data


@pytest.mark.parametrize(("query", "statement", "expected_terms"), [
    ("café délai", "Le café applique un délai maximal de cinq secondes.", ["café", "délai"]),
    ("北辰缓存", "北辰缓存容量为512条记录。", ["北辰", "缓存", "辰缓"]),
])
def test_multilingual_http_evidence_remains_attributed_and_not_falsely_abstained(
    corpus: Corpus, endpoint: str, query: str, statement: str, expected_terms: list[str],
) -> None:
    source, _ = hierarchical.add_fact(corpus.root, "Instrument observation: " + statement, scopes=[SCOPE])
    receipt = bind_evidence(corpus.root, "fact", source.id)
    target, _ = hierarchical.add_fact(corpus.root, statement, scopes=[SCOPE], evidence=[receipt])
    status, data = http(endpoint, {"question": query, "channels": ["facts"],
                                   "fact_scorer": "bm25-v1", "evidence_budget": 256})
    assert status == 200 and data["fact_scorer"] == "bm25-v1"
    assert data["assessment"]["abstain"] is False
    item = next(item for item in data["items"] if item["id"] == "fact:" + target.id)
    assert item["text"].splitlines()[0] == statement
    assert item["provenance"]["search"]["matched_terms"] == expected_terms
    assert any(node["quote"] == source.statement for node in item["provenance"]["evidence_context"]["nodes"])


@pytest.mark.parametrize("arguments", [("fact", "search", QUERY, "--scorer", "unknown-v1"),
                                       ("recall", QUERY, "--fact-scorer", "unknown-v1", "--json")])
def test_cli_rejects_unknown_scorer_without_a_traceback(corpus: Corpus, arguments: tuple[str, ...]) -> None:
    result = cli(corpus, *arguments)
    assert result.returncode == 2
    assert "unknown-v1" in result.stderr and "Traceback" not in result.stderr


@pytest.mark.parametrize("scorer", SCORERS)
def test_public_mcp_tools_transport_scorer_and_preserve_governed_result_limits(corpus: Corpus, scorer: str) -> None:
    pytest.importorskip("mcp")
    from commontrace import mcp_server

    server = mcp_server.build_server(corpus.root)
    for name in ("query_facts", "archival_memory_search"):
        data = tool(server, name, query=QUERY, scope=SCOPE, limit=1, scorer=scorer)
        assert data["ok"] and data["count"] == 1
        assert data["facts"][0]["fact"]["id"] == corpus.target.id
        assert "SECRET_BETA" not in json.dumps(data)
    recalled = tool(server, "memory_recall", question=QUERY, channels=["facts"], scope=SCOPE,
                    fact_scorer=scorer, evidence_budget=256)
    assert recalled["ok"]
    assert_admitted(corpus, recalled, scorer)
    hierarchical.delete_fact(corpus.root, corpus.source.id)
    withdrawn = tool(server, "query_facts", query=QUERY, scope=SCOPE, scorer=scorer)
    assert withdrawn["ok"]
    assert corpus.target.id not in {row["fact"]["id"] for row in withdrawn["facts"]}


def test_mcp_schemas_expose_default_profile_and_unknown_names_fail(corpus: Corpus) -> None:
    pytest.importorskip("mcp")
    from commontrace import mcp_server

    server = mcp_server.build_server(corpus.root)
    schemas = {item.name: getattr(item, "input_schema", getattr(item, "inputSchema", None))
               for item in asyncio.run(server.list_tools())}
    for name in ("query_facts", "archival_memory_search"):
        assert schemas[name]["properties"]["scorer"]["default"] == "overlap-v1"
        result = tool(server, name, query=QUERY, scorer="unknown-v1")
        assert result["ok"] is False and "facts" not in result
    assert schemas["memory_recall"]["properties"]["fact_scorer"]["default"] == "overlap-v1"
    result = tool(server, "memory_recall", question=QUERY, fact_scorer="unknown-v1")
    assert result["ok"] is False and "items" not in result
