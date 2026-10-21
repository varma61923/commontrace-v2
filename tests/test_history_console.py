from __future__ import annotations

import json
from pathlib import Path

import pytest

from unimem import gateway, trace_io, workbench

TOKEN = "history-contract-credential"
AUTH = {"Authorization": "Bearer " + TOKEN}


def call(app, path, headers=AUTH):
    response = app.handle("GET", path, headers)
    return response.status, json.loads(response.body)


def test_history_empty_and_authenticated(tmp_path):
    app = gateway.Gateway(str(tmp_path), token=TOKEN)
    for route, field in (("traces", "traces"), ("benchmark-history", "reports")):
        assert call(app, "/v1/" + route, {})[0] == 401
        status, data = call(app, "/v1/" + route)
        assert status == 200 and data[field] == [] and data["total"] == 0
    assert not (tmp_path / "memory").exists()


def test_trace_page_detail_redaction_and_traversal(tmp_path):
    path = trace_io.write_new(str(tmp_path), title="Retry contract", context="Timeout on payment",
                              solution="Keep the same request identifier", tags=["retry"])
    app = gateway.Gateway(str(tmp_path), token=TOKEN)
    status, data = call(app, "/v1/traces")
    assert status == 200 and data["total"] == 1
    name = Path(path).name
    assert data["traces"][0]["file"] == name
    assert "body" not in data["traces"][0]
    status, detail = call(app, "/v1/trace?file=" + name)
    assert status == 200 and "same request identifier" in detail["body"]
    for name in ("../secrets.md", "%2Fetc%2Fpasswd", "", "a.json"):
        assert call(app, "/v1/trace?file=" + name)[0] == 400
    assert call(app, "/v1/trace?file=missing.md")[0] == 404
    with open(path, "a") as output:
        output.write("\nOPENAI_API_KEY=sk-" + "a" * 48)
    assert "sk-" + "a" * 48 not in call(app, "/v1/trace?file=" + Path(path).name)[1]["body"]


def test_history_rejects_symlink_directories_leaves_and_large_files(tmp_path):
    directory = tmp_path / "memory" / "traces"
    directory.mkdir(parents=True)
    outside = tmp_path / "private.md"
    outside.write_text("private data")
    (directory / "link.md").symlink_to(outside)
    (directory / "large.md").write_bytes(b"x" * (workbench.HISTORY_FILE_BYTES + 1))
    app = gateway.Gateway(str(tmp_path), token=TOKEN)
    assert call(app, "/v1/traces")[1]["skipped"] == 1
    assert call(app, "/v1/trace?file=link.md")[0] == 403
    directory.rename(tmp_path / "actual-traces")
    directory.symlink_to(tmp_path / "actual-traces", target_is_directory=True)
    assert call(app, "/v1/traces")[0] == 403
    assert call(app, "/v1/trace?file=large.md")[0] == 403


def test_saved_reports_trends_use_only_actual_finite_metrics(tmp_path):
    directory = tmp_path / "memory" / "benchmark_reports"
    directory.mkdir(parents=True)
    reports = [
        {"lesson_quality": {"value": .5}, "transfer_gap": {"value": -.2}},
        {"lesson_quality": {"value": .75}, "implicit_retrieval": {"strict": float("nan"), "permissive": True}},
    ]
    for index, report in enumerate(reports):
        (directory / f"2026-10-14_00000{index}.json").write_text(json.dumps(report))
    (directory / "2026-10-14_000002.json").write_text("not JSON")
    app = gateway.Gateway(str(tmp_path), token=TOKEN)
    status, data = call(app, "/v1/benchmark-history")
    assert status == 200 and data["total"] == 3 and data["skipped"] == 1
    assert [row["metrics"]["lesson_quality"] for row in data["reports"]] == [.5, .75]
    assert data["reports"][1]["delta"]["lesson_quality"] == .25
    assert data["reports"][1]["metrics"]["implicit_retrieval_strict"] is None
    assert data["reports"][1]["metrics"]["implicit_retrieval_permissive"] is None
    assert data["reports"][0]["delta"]["transfer_gap"] is None
    assert call(app, "/v1/benchmark-history?limit=1")[1]["reports"] == []


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=garbage", "offset=-1", "offset=10001"])
def test_history_pagination_validation(tmp_path, query):
    assert call(gateway.Gateway(str(tmp_path), token=TOKEN), "/v1/traces?" + query)[0] == 400


def test_operator_history_refuses_container_scope(tmp_path):
    from unimem import telemetry

    app = gateway.Gateway(str(tmp_path), token=TOKEN)
    with telemetry.bind(container_tag="tenant-a"):
        for path in ("/v1/traces", "/v1/trace?file=a.md", "/v1/benchmark-history"):
            assert call(app, path, {**AUTH, "X-Container-Tag": "tenant-a"})[0] == 403


def test_history_openapi_response_contracts(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from referencing import Registry, Resource

    app = gateway.Gateway(str(tmp_path), token=TOKEN)
    document = call(app, "/v1/openapi.json", {})[1]
    for path in ("/v1/traces", "/v1/benchmark-history"):
        response_schema = document["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        schema = {**response_schema, "components": document["components"]}
        resource = Resource.from_contents(schema, default_specification=__import__("referencing.jsonschema", fromlist=["DRAFT4"]).DRAFT4)
        jsonschema.Draft4Validator(schema, registry=Registry().with_resource("", resource)).validate(call(app, path)[1])


def test_oversized_numeric_metric_degrades_without_breaking_history(tmp_path):
    directory = tmp_path / "memory" / "benchmark_reports"
    directory.mkdir(parents=True)
    (directory / "2026-10-14.json").write_text(json.dumps({"lesson_quality": {"value": 10 ** 500}}))
    status, data = call(gateway.Gateway(str(tmp_path), token=TOKEN), "/v1/benchmark-history")
    assert status == 200 and data["reports"][0]["metrics"]["lesson_quality"] is None
