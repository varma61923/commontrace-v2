"""Reading four other systems' exports without a transform script.

What these tests defend, in rough order of how badly getting it wrong would
hurt a customer:

1. **Nothing is invented.** A row missing its solution text must be SKIPPED
   with a reason, never imported with a plausible placeholder. A bulk import
   is someone else's data arriving in bulk, and one fabricated field
   repeated ten thousand times becomes a corpus this product then measures
   and bills against.
2. **Silence is not success.** OTel's UNSET status, a LangSmith run with no
   `error` key, a Langfuse trace with no recognised score -- none of these
   may read as "resolved". Scoring an uninstrumented fleet as 100% resolved
   is the single most expensive wrong answer available here, because it
   feeds the causal machinery.
3. **Both export shapes work.** OTLP-JSON's attribute LIST and the flat
   attribute dict are both real, and supporting one of them means reading
   zero rows from a file that obviously looks fine to a human.
4. **A misspelled --source is an error**, not a silent pass-through that
   imports ten thousand empty traces.
5. **The whole path, not just the mapper**: these go through
   `commontrace import` for the same reason the MCP tests go through
   `call_tool` -- an adapter nothing calls is not shipped.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from commontrace import adapters, frontmatter, import_data, paths

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def norm(row: dict, source: str) -> dict:
    return adapters.normalize(row, source)


# --- LangSmith ---------------------------------------------------------------

class TestLangSmith:
    RUN = {
        "id": "run-1",
        "name": "AgentExecutor",
        "run_type": "chain",
        "inputs": {"input": "customer cannot reset their password"},
        "outputs": {"output": "removed the suppression and re-sent"},
        "error": None,
        "tags": ["support", "email"],
        "total_tokens": 1320,
    }

    def test_nested_inputs_and_outputs_are_reached(self):
        flat = norm(self.RUN, "langsmith")
        assert "cannot reset their password" in flat["context"]
        assert "re-sent" in flat["solution"]

    def test_the_title_is_specific_not_the_chain_name(self):
        """Every row from one pipeline exports as "AgentExecutor"; importing
        ten thousand gives a store whose traces are indistinguishable in
        every listing a curator reads."""
        flat = norm(self.RUN, "langsmith")
        assert flat["title"].startswith("AgentExecutor: ")
        assert "cannot reset their password" in flat["title"]

    def test_a_null_error_on_a_finished_run_is_a_success(self):
        assert norm(self.RUN, "langsmith")["resolved"] is True

    def test_an_error_is_a_failure_and_its_text_survives(self):
        """LangSmith already knows which runs failed, and that label is
        exactly what distillation is looking for."""
        flat = norm({**self.RUN, "error": "ToolException: approval API 500"}, "langsmith")
        assert flat["resolved"] is False
        assert "approval API 500" in flat["context"]

    def test_a_run_with_no_error_field_gets_no_outcome(self):
        """An unreported occasion is missing data, not a win."""
        run = {k: v for k, v in self.RUN.items() if k != "error"}
        assert "resolved" not in norm(run, "langsmith")

    def test_tokens_and_source_id_are_carried(self):
        flat = norm(self.RUN, "langsmith")
        assert flat["tokens_used"] == 1320
        assert flat["id"] == "run-1"

    def test_tokens_from_nested_metadata_are_found(self):
        run = {k: v for k, v in self.RUN.items() if k != "total_tokens"}
        run["extra"] = {"metadata": {"total_tokens": 99}}
        assert norm(run, "langsmith")["tokens_used"] == 99


# --- Langfuse ----------------------------------------------------------------

class TestLangfuse:
    TRACE = {
        "id": "tr-1",
        "name": "support-agent",
        "input": "refund request over the limit",
        "output": "escalated to a manager",
        "tags": ["billing"],
        "scores": [{"name": "correctness", "value": 1}],
        "usage": {"total": 800},
    }

    def test_input_output_and_tags_are_mapped(self):
        flat = norm(self.TRACE, "langfuse")
        assert "refund request" in flat["context"]
        assert "escalated" in flat["solution"]
        assert flat["tags"] == ["billing"]

    def test_a_success_score_becomes_the_outcome(self):
        assert norm(self.TRACE, "langfuse")["resolved"] is True

    def test_a_zero_success_score_is_a_failure(self):
        flat = norm(
            {**self.TRACE, "scores": [{"name": "correctness", "value": 0}]}, "langfuse")
        assert flat["resolved"] is False

    def test_an_unrecognised_score_name_is_not_guessed_at(self):
        """A fleet with a scorer called `toxicity` would have every safe
        answer read as a failure by anything that took the first numeric
        score it found."""
        flat = norm(
            {**self.TRACE, "scores": [{"name": "toxicity", "value": 0}]}, "langfuse")
        assert "resolved" not in flat

    def test_an_error_level_is_a_failure(self):
        trace = {k: v for k, v in self.TRACE.items() if k != "scores"}
        assert norm({**trace, "level": "ERROR"}, "langfuse")["resolved"] is False

    def test_a_boolean_score_is_read_as_itself(self):
        flat = norm(
            {**self.TRACE, "scores": [{"name": "resolved", "value": False}]}, "langfuse")
        assert flat["resolved"] is False

    def test_usage_totals_are_carried(self):
        assert norm(self.TRACE, "langfuse")["tokens_used"] == 800


# --- Braintrust --------------------------------------------------------------

class TestBraintrust:
    SPAN = {
        "id": "sp-1",
        "input": "classify this ticket",
        "output": "billing",
        "expected": "account-access",
        "span_attributes": {"name": "classify"},
        "scores": {"correctness": 0.0},
        "metrics": {"prompt_tokens": 100, "completion_tokens": 20},
    }

    def test_expected_is_carried_when_it_disagrees_with_output(self):
        """A row where output and expected disagree is a labelled failure,
        and labelled failures are what this product distils lessons from."""
        flat = norm(self.SPAN, "braintrust")
        assert "billing" in flat["solution"]
        assert "account-access" in flat["solution"]

    def test_a_matching_expected_is_not_repeated(self):
        flat = norm({**self.SPAN, "expected": "billing"}, "braintrust")
        assert flat["solution"].count("billing") == 1

    def test_a_failing_score_is_a_failure(self):
        assert norm(self.SPAN, "braintrust")["resolved"] is False

    def test_a_partial_score_is_not_a_pass(self):
        """Braintrust scores are 0..1; a >0 test would read 0.05 as
        success."""
        flat = norm({**self.SPAN, "scores": {"correctness": 0.05}}, "braintrust")
        assert flat["resolved"] is False

    def test_a_full_score_is_a_pass(self):
        flat = norm({**self.SPAN, "scores": {"correctness": 1.0}}, "braintrust")
        assert flat["resolved"] is True

    def test_token_metrics_are_summed_when_there_is_no_total(self):
        assert norm(self.SPAN, "braintrust")["tokens_used"] == 120


# --- OpenTelemetry -----------------------------------------------------------

_OTLP_SPAN = {
    "name": "chat gpt-4",
    "traceId": "abc",
    "spanId": "s1",
    "status": {"code": "STATUS_CODE_ERROR", "message": "rate limited"},
    "attributes": [
        {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
        {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4"}},
        {"key": "gen_ai.prompt", "value": {"stringValue": "summarise the ticket"}},
        {"key": "gen_ai.completion", "value": {"stringValue": "a partial summary"}},
        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": 120}},
        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": 40}},
    ],
}

_FLAT_SPAN = {
    "name": "chat gpt-4",
    "span_id": "s2",
    "status_code": "OK",
    "attributes": {
        "gen_ai.system": "anthropic",
        "traceloop.entity.input": "summarise the ticket",
        "traceloop.entity.output": "a full summary",
        "gen_ai.usage.prompt_tokens": 10,
        "gen_ai.usage.completion_tokens": 5,
    },
}


class TestOtel:
    def test_otlp_json_attribute_lists_are_unwrapped(self):
        flat = norm(_OTLP_SPAN, "otel")
        assert "summarise the ticket" in flat["context"]
        assert "a partial summary" in flat["solution"]

    def test_flat_attribute_dicts_work_too(self):
        """Supporting one shape means reading zero rows from a file that
        obviously looks fine to a human."""
        flat = norm(_FLAT_SPAN, "otel")
        assert "summarise the ticket" in flat["context"]
        assert "a full summary" in flat["solution"]

    def test_an_error_status_is_a_failure_and_the_message_survives(self):
        flat = norm(_OTLP_SPAN, "otel")
        assert flat["resolved"] is False
        assert "rate limited" in flat["context"]

    def test_an_ok_status_is_a_success(self):
        assert norm(_FLAT_SPAN, "otel")["resolved"] is True

    def test_an_unset_status_is_not_a_success(self):
        """UNSET is the default every span carries whether or not anything
        checked it. Treating it as a pass would score an entire
        uninstrumented fleet as 100% resolved."""
        span = {**_OTLP_SPAN, "status": {"code": "STATUS_CODE_UNSET"}}
        assert "resolved" not in norm(span, "otel")

    def test_a_span_with_no_status_at_all_is_not_a_success(self):
        span = {k: v for k, v in _OTLP_SPAN.items() if k != "status"}
        assert "resolved" not in norm(span, "otel")

    def test_both_token_attribute_spellings_are_summed(self):
        assert norm(_OTLP_SPAN, "otel")["tokens_used"] == 160
        assert norm(_FLAT_SPAN, "otel")["tokens_used"] == 15

    def test_the_model_and_system_become_tags(self):
        assert set(norm(_OTLP_SPAN, "otel")["tags"]) == {"openai", "gpt-4"}

    def test_attribute_unwrapping_handles_arrays(self):
        span = {
            "name": "s",
            "attributes": [
                {"key": "k", "value": {"arrayValue": {"values": [
                    {"stringValue": "a"}, {"stringValue": "b"},
                ]}}},
            ],
        }
        assert adapters.otel_attributes(span) == {"k": ["a", "b"]}

    def test_attribute_unwrapping_handles_each_scalar_wrapper(self):
        span = {"attributes": [
            {"key": "s", "value": {"stringValue": "x"}},
            {"key": "i", "value": {"intValue": 3}},
            {"key": "d", "value": {"doubleValue": 1.5}},
            {"key": "b", "value": {"boolValue": True}},
        ]}
        assert adapters.otel_attributes(span) == {
            "s": "x", "i": 3, "d": 1.5, "b": True,
        }


# --- the registry and the shared path ----------------------------------------

class TestRegistry:
    def test_a_misspelled_source_is_an_error(self):
        """Importing ten thousand rows as `generic` because a flag was
        misspelled produces a store full of traces with no text in them and
        no indication why."""
        with pytest.raises(ValueError, match="unknown import source"):
            adapters.normalize({"a": 1}, "langsmth")

    def test_the_error_lists_the_real_sources(self):
        with pytest.raises(ValueError) as exc:
            adapters.normalize({}, "nope")
        for name in ("langsmith", "langfuse", "braintrust", "otel"):
            assert name in str(exc.value)

    def test_generic_passes_a_flat_row_through_untouched(self):
        row = {"title": "t", "context": "c", "solution": "s"}
        assert adapters.normalize(row, adapters.GENERIC) == row

    def test_every_source_describes_itself_for_the_help_text(self):
        for name, adapter in adapters.ADAPTERS.items():
            assert adapter.describe, name

    def test_every_adapter_survives_an_empty_row(self):
        """An export with a blank or unexpected line must skip that row, not
        crash the whole import."""
        for name in adapters.SOURCES:
            assert isinstance(adapters.normalize({}, name), dict)

    def test_a_non_dict_row_is_not_a_crash(self):
        assert adapters.normalize([], "langsmith") == {}


class TestSharedImportPath:
    """The adapter runs inside `import_data`, so it inherits the streaming,
    the skip reasons and the schema validation rather than bringing its own."""

    def test_a_row_missing_its_solution_is_skipped_with_a_reason(self):
        mapping = import_data.FieldMapping(source="langsmith")
        lines = iter([json.dumps({"id": "r", "name": "n", "inputs": {"i": "x"}})])
        results = list(import_data.iter_jsonl(lines, mapping))
        assert len(results) == 1
        assert isinstance(results[0], import_data.SkippedRow)
        assert "solution" in results[0].reason

    def test_a_good_row_becomes_an_imported_row_with_its_outcome(self):
        mapping = import_data.FieldMapping(source="langsmith")
        lines = iter([json.dumps(TestLangSmith.RUN)])
        results = list(import_data.iter_jsonl(lines, mapping))
        assert isinstance(results[0], import_data.ImportedRow)
        assert results[0].outcome["resolved"] is True
        assert results[0].outcome["tokens_used"] == 1320

    def test_the_default_source_is_unchanged_behaviour(self):
        assert import_data.FieldMapping().source == adapters.GENERIC


# --- through the CLI ---------------------------------------------------------

def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "support").returncode == 0
    return root


def _write(tmp_path, name: str, rows: list[dict]) -> str:
    path = str(tmp_path / name)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


class TestThroughTheCLI:
    def test_a_langsmith_export_imports_with_no_field_flags(self, tmp_path, store):
        """The whole point: a customer with two years of history should not
        have to write a transform script first."""
        export = _write(tmp_path, "runs.jsonl", [
            TestLangSmith.RUN,
            {**TestLangSmith.RUN, "id": "run-2", "error": "boom",
             "inputs": {"input": "refund over the limit"}},
        ])
        result = cli("import", export, "--source", "langsmith",
                     "--agent-type", "support", "--dest", store)
        assert result.returncode == 0, result.stderr
        assert "2 row(s) parseable" in result.stdout

        written = [
            f for f in os.listdir(paths.traces_dir(store)) if f.endswith(".md")
            and f != "README.md"
        ]
        assert len(written) == 2
        outcomes = []
        for name in written:
            fm, _ = frontmatter.read(os.path.join(paths.traces_dir(store), name))
            outcomes.append(fm.get("outcome", {}).get("resolved"))
        assert sorted(outcomes, key=str) == [False, True]

    def test_an_otel_export_imports(self, tmp_path, store):
        export = _write(tmp_path, "spans.jsonl", [_OTLP_SPAN, _FLAT_SPAN])
        result = cli("import", export, "--source", "otel",
                     "--agent-type", "support", "--dest", store)
        assert result.returncode == 0, result.stderr
        assert "2 row(s) parseable" in result.stdout

    def test_dry_run_writes_nothing(self, tmp_path, store):
        export = _write(tmp_path, "runs.jsonl", [TestLangSmith.RUN])
        before = set(os.listdir(paths.traces_dir(store)))
        result = cli("import", export, "--source", "langsmith", "--dry-run",
                     "--agent-type", "support", "--dest", store)
        assert result.returncode == 0, result.stderr
        assert set(os.listdir(paths.traces_dir(store))) == before

    def test_the_source_id_is_recorded_for_traceability(self, tmp_path, store):
        export = _write(tmp_path, "runs.jsonl", [TestLangSmith.RUN])
        assert cli("import", export, "--source", "langsmith",
                   "--agent-type", "support", "--dest", store).returncode == 0
        bodies = []
        for name in os.listdir(paths.traces_dir(store)):
            if name.endswith(".md") and name != "README.md":
                with open(os.path.join(paths.traces_dir(store), name)) as fh:
                    bodies.append(fh.read())
        assert any("run-1" in b for b in bodies)

    def test_every_source_is_offered_by_the_help(self, store):
        help_text = cli("import", "--help").stdout
        for name in adapters.SOURCES:
            assert name in help_text

    def test_the_help_says_no_network_call_is_made(self, store):
        """A security reviewer reading `--help` is the first person who asks
        whether this opens a socket."""
        assert "nothing leaves your machine" in cli("import", "--help").stdout
