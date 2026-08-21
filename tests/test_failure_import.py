"""Reading a fleet's failures out of data it already has.

This is the module that decides whether the commons thesis is testable
before adoption or only after it. Everything here is about a prospect's
real export -- messy columns, duplicate alerts, whatever their tool emits
-- turning into the same signatures a `memory/traces/` store would produce.

Two properties matter more than the parsing:

1. A signature from an import must equal a signature from the equivalent
   trace. If they diverge, every coverage number stays plausible and
   becomes meaningless, which is worse than an error.
2. What was measured must be stated. A 400-row collapse or a 500-row cap
   changes what the coverage fraction describes.
"""
from __future__ import annotations

import json
import os

import pytest

from commontrace import failure_import, overlap
from commontrace.commands import commons_cmd


def _write(tmp_path, name, text):
    p = os.path.join(str(tmp_path), name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


class TestFormats:
    """A prospect should not have to reshape their export before they can
    get a number. Every format here is one somebody actually exports."""

    def test_plain_lines(self, tmp_path):
        p = _write(tmp_path, "alerts.txt",
                   "Connection pool exhausted\nWebhook delivered twice\n\nPod killed mid-request\n")
        failures, stats = failure_import.read_failures(p)
        assert stats["format"] == "lines"
        assert [f["label"] for f in failures] == [
            "Connection pool exhausted", "Webhook delivered twice", "Pod killed mid-request",
        ]

    def test_markdown_bullets_are_lines(self, tmp_path):
        """A postmortem index pasted straight out of a wiki."""
        p = _write(tmp_path, "index.md", "- Pool exhausted under load\n* Duplicate charge\n")
        failures, _ = failure_import.read_failures(p)
        assert [f["label"] for f in failures] == ["Pool exhausted under load", "Duplicate charge"]

    def test_jsonl(self, tmp_path):
        p = _write(tmp_path, "incidents.jsonl",
                   '{"title": "Pool exhausted", "description": "spike drained it", '
                   '"tags": ["db", "load"]}\n'
                   '{"title": "Clock drift 401s", "description": "token rejected"}\n')
        failures, stats = failure_import.read_failures(p)
        assert stats["format"] == "jsonl"
        assert failures[0]["tags"] == ["db", "load"]
        assert failures[0]["text"] == "spike drained it"

    def test_json_array(self, tmp_path):
        p = _write(tmp_path, "export.json",
                   json.dumps([{"summary": "Pool exhausted", "detail": "spike"}]))
        failures, stats = failure_import.read_failures(p)
        assert stats["format"] == "json"
        assert failures[0]["label"] == "Pool exhausted"

    def test_json_envelope(self, tmp_path):
        """Issue trackers wrap the array; refusing that would send someone
        to jq before they can evaluate the product."""
        p = _write(tmp_path, "export.json",
                   json.dumps({"total": 2, "issues": [{"summary": "A thing broke"}]}))
        failures, _ = failure_import.read_failures(p)
        assert failures[0]["label"] == "A thing broke"

    def test_csv_with_arbitrary_column_names(self, tmp_path):
        p = _write(tmp_path, "tickets.csv",
                   "Key,Summary,Description,Components\n"
                   "OPS-1,Pool exhausted,spike drained it,\"db,load\"\n")
        failures, stats = failure_import.read_failures(p)
        assert stats["format"] == "csv"
        assert failures[0]["label"] == "Pool exhausted"
        assert failures[0]["tags"] == ["db", "load"]

    def test_tsv(self, tmp_path):
        p = _write(tmp_path, "t.tsv", "title\tdescription\nPool exhausted\tspike\n")
        failures, _ = failure_import.read_failures(p)
        assert failures[0]["label"] == "Pool exhausted"

    def test_a_description_only_row_is_still_usable(self, tmp_path):
        """The title is only a human-facing label in the report; a row with
        just a body still carries the signal being matched."""
        p = _write(tmp_path, "x.jsonl", '{"description": "the pool ran dry during a spike"}\n')
        failures, _ = failure_import.read_failures(p)
        assert failures[0]["text"] == "the pool ran dry during a spike"


class TestWhatWasMeasuredIsStated:
    def test_exact_duplicates_are_collapsed_and_counted(self, tmp_path):
        """400 copies of one alert is what 'recurring' looks like in raw
        data. Left in, the coverage fraction describes that alert rather
        than the fleet."""
        p = _write(tmp_path, "a.txt", "Pool exhausted\n" * 10 + "Clock drift\n")
        failures, stats = failure_import.read_failures(p)
        assert stats["rows"] == 11
        assert stats["unique"] == 2
        assert stats["deduplicated"] == 9

    def test_dedup_ignores_case_and_whitespace(self, tmp_path):
        p = _write(tmp_path, "a.txt", "Pool  Exhausted\npool exhausted\n")
        _, stats = failure_import.read_failures(p)
        assert stats["unique"] == 1

    def test_oversized_input_is_capped_and_says_so(self, tmp_path):
        p = _write(tmp_path, "a.txt",
                   "\n".join(f"distinct failure number {i}" for i in range(600)))
        failures, stats = failure_import.read_failures(p)
        assert len(failures) == failure_import.MAX_FAILURES
        assert stats["truncated"] == 600 - failure_import.MAX_FAILURES


class TestRefusalsAreActionable:
    """'Invalid input' sends someone to Slack instead of to a number."""

    def test_missing_file(self, tmp_path):
        with pytest.raises(failure_import.FailureImportError, match="cannot read"):
            failure_import.read_failures(os.path.join(str(tmp_path), "nope.txt"))

    def test_empty_file(self, tmp_path):
        with pytest.raises(failure_import.FailureImportError, match="empty"):
            failure_import.read_failures(_write(tmp_path, "a.txt", "   \n"))

    def test_malformed_jsonl_names_the_line(self, tmp_path):
        p = _write(tmp_path, "a.jsonl", '{"title": "ok"}\nnot json at all\n')
        with pytest.raises(failure_import.FailureImportError, match="line 2"):
            failure_import.read_failures(p)

    def test_csv_without_a_usable_column_lists_what_it_wanted(self, tmp_path):
        p = _write(tmp_path, "a.csv", "id,owner,priority\n1,alice,P2\n")
        with pytest.raises(failure_import.FailureImportError) as exc:
            failure_import.read_failures(p)
        assert "title" in str(exc.value) and "owner" in str(exc.value)


class TestSignaturesAreComparable:
    def test_an_imported_failure_signs_identically_to_the_same_trace(self, tmp_path):
        """The property the whole feature rests on. If an imported
        signature and a store signature are computed differently, every
        number stays plausible and means nothing."""
        title, text, tags = "Pool exhausted", "spike drained it", ["db", "load"]
        p = _write(tmp_path, "a.jsonl", json.dumps(
            {"title": title, "description": text, "tags": tags}) + "\n")
        signed, _ = commons_cmd.signatures_from_file(p)
        reference = overlap.minhash(
            " ".join([title, text, " ".join(tags)]), commons_cmd.COMMONS_NUM_PERM,
        )
        assert signed[0]["signature"] == reference

    def test_it_matches_the_hub_side_signature(self, tmp_path):
        """The Hub signs title+context+tags via hub/commons.py. An imported
        failure must land in the same space or commons_overlap compares
        mismatched positions and returns a confident wrong number."""
        hub_commons = pytest.importorskip("hub.commons")
        title, text, tags = "Pool exhausted", "spike drained it", ["db", "load"]
        p = _write(tmp_path, "a.jsonl", json.dumps(
            {"title": title, "description": text, "tags": tags}) + "\n")
        signed, _ = commons_cmd.signatures_from_file(p)
        assert signed[0]["signature"] == hub_commons.signature_for(title, text, tags)

    def test_no_failure_text_is_in_the_signed_output(self, tmp_path):
        """Labels travel by design; bodies must not. This is the claim made
        to a prospect at the moment they decide whether to send the file."""
        p = _write(tmp_path, "a.jsonl", json.dumps(
            {"title": "Pool exhausted", "description": "SECRETCUSTOMERNAME went down"}) + "\n")
        signed, _ = commons_cmd.signatures_from_file(p)
        assert "SECRETCUSTOMERNAME" not in json.dumps(signed)
