"""Differential test: parse_yaml_minimal vs real PyYAML.

measure_performance.py carries a hand-rolled YAML subset parser so that
`python measure_performance.py` works when executed directly in an
environment without PyYAML installed. Its docstring makes specific
behavioural claims -- scientific-notation handling, quote escaping, wrapped
scalars -- and a claim CI does not check is just a comment.

The contract under test is narrow and worth stating: for any frontmatter
*this project's own writer emits*, the fallback must produce exactly what
PyYAML produces. It is not a general YAML implementation and does not try to
be; the documented gaps are asserted here too, so they stay deliberate rather
than becoming surprises.
"""
import datetime
import random

import measure_performance as mp
import pytest
import yaml


def _roundtrip(obj):
    """Dump with PyYAML the way the project writes files, then parse both ways."""
    text = yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)
    return yaml.safe_load(text), mp.parse_yaml_minimal(text), text


def _assert_agrees(obj):
    reference, fallback, text = _roundtrip(obj)
    assert fallback == reference, (
        f"fallback diverged from PyYAML\n--- yaml ---\n{text}"
        f"--- pyyaml ---\n{reference}\n--- fallback ---\n{fallback}"
    )


class TestScalarTypes:
    @pytest.mark.parametrize("value", [
        "plain string", "", "with: colon", "trailing space ", "  leading",
        "quote'inside", 'double"inside', "hash # not a comment",
        0, 1, -1, 42, 999999,
        0.0, 1.5, -2.75,
        True, False, None,
    ])
    def test_scalar_matches_pyyaml(self, value):
        _assert_agrees({"k": value})

    @pytest.mark.parametrize("literal", [
        "7E3", "7.0e3", "7.0e+3", "1e5", "1.5e-3",
        "0", "007", "010", "0x1f", "0b101", "1_000", "-42", "+7",
    ])
    def test_ambiguous_numeric_literals_resolve_identically(self, literal):
        """PyYAML's YAML-1.1 resolvers, which are easy to get backwards.

        Floats need BOTH a literal '.' and a *signed* exponent, so '7E3' and
        '7.0e3' are both strings while '7.0e+3' is a float. Integers include
        bare-leading-zero octal, so '010' is 8. Both classes of mistake are
        silent: they yield a plausible wrong value rather than an error.

        Ground truth is read from PyYAML here rather than hard-coded, because
        the hard-coded version of this expectation was wrong.
        """
        text = f"k: {literal}\n"
        assert mp.parse_yaml_minimal(text) == yaml.safe_load(text)

    def test_dates_match(self):
        _assert_agrees({"d": datetime.date(2026, 8, 20)})


class TestCollections:
    def test_block_list_of_scalars(self):
        _assert_agrees({"tags": ["a", "b", "c"]})

    def test_empty_list_and_empty_dict(self):
        _assert_agrees({"tags": [], "meta": {}})

    def test_nested_mapping(self):
        _assert_agrees({"outcome": {"resolved": True, "tokens_used": 1200, "note": "ok"}})

    def test_list_of_dicts(self):
        _assert_agrees({"importance_history": [
            {"date": "2026-01-01", "value": 3, "why": "initial"},
            {"date": "2026-02-01", "value": 4, "why": "raised"},
        ]})

    def test_deeply_nested_mapping(self):
        _assert_agrees({"a": {"b": {"c": {"d": "leaf"}}}})


class TestWrappedScalars:
    def test_a_long_scalar_wrapped_by_pyyaml_folds_back_to_one_line(self):
        """PyYAML wraps at width=80 by default, so any long description in a
        real lesson file arrives as continuation lines."""
        long_text = " ".join(f"word{i}" for i in range(60))
        _assert_agrees({"description": long_text})

    def test_a_long_quoted_scalar(self):
        _assert_agrees({"description": "it's " + " ".join(f"w{i}" for i in range(50))})


class TestRealisticFrontmatter:
    def test_a_full_lesson_record(self):
        _assert_agrees({
            "name": "lesson_refund_posting_window",
            "description": "Confirm the provider settlement id before promising a refund date, "
                           "then explain the 5-10 day bank posting window to the customer",
            "tags": ["refunds", "escalation"],
            "agent_type": "support",
            "domain": "escalation",
            "importance": 4,
            "importance_rationale": "prevents a chargeback and a repeat contact",
            "importance_history": [{"date": "2026-08-01", "value": 3}],
            "applies_when": "a customer reports an approved refund has not arrived",
            "do_not_apply_when": "the refund was never approved",
            "uses": 12,
            "last_hit": "2026-08-19",
            "source_traces": ["a2432eac", "cb7ad72e"],
            "status": "active",
        })

    def test_a_full_trace_record(self):
        _assert_agrees({
            "id": "6e498b81-061d-4b76-9b02-04e91afc9d3b",
            "title": "Refund delayed past SLA",
            "agent_type": "support",
            "tags": ["refunds"],
            "profile": "",
            "created_at": "2026-08-20T02:16:26",
            "outcome": {"resolved": True, "escalated": False, "tokens_used": 1200,
                        "llm_calls": 4, "baseline": False},
        })


class TestGeneratedCases:
    """The 'hundreds of generated cases' the docstring claims, actually run."""

    def _random_scalar(self, rng):
        return rng.choice([
            rng.choice(["ok", "with: colon", "a'b", 'a"b', "trailing ", "# hash", ""]),
            rng.randint(-1000, 1000),
            round(rng.uniform(-100, 100), 3),
            rng.choice([True, False, None]),
            " ".join(f"w{i}" for i in range(rng.randint(1, 40))),
        ])

    def _random_doc(self, rng, depth=0):
        doc = {}
        for i in range(rng.randint(1, 6)):
            key = f"key{i}"
            roll = rng.random()
            if roll < 0.15 and depth < 2:
                doc[key] = self._random_doc(rng, depth + 1)
            elif roll < 0.30:
                doc[key] = [self._random_scalar(rng) for _ in range(rng.randint(0, 4))]
            elif roll < 0.38 and depth < 2:
                doc[key] = [self._random_doc(rng, depth + 1) for _ in range(rng.randint(1, 3))]
            else:
                doc[key] = self._random_scalar(rng)
        return doc

    def test_three_hundred_generated_documents_agree(self):
        rng = random.Random(20260820)  # fixed seed: a failure is reproducible
        for _ in range(300):
            _assert_agrees(self._random_doc(rng))


class TestDocumentedGaps:
    """The docstring lists deliberate gaps. Asserting them keeps them
    deliberate -- if one is ever closed, this test fails and the docstring
    gets updated rather than quietly going stale."""

    def test_a_list_nested_directly_in_a_list_is_a_known_gap(self):
        text = "k:\n- - inner\n"
        assert yaml.safe_load(text) == {"k": [["inner"]]}
        assert mp.parse_yaml_minimal(text) != yaml.safe_load(text)

    def test_a_numeric_mapping_key_reads_back_as_a_string(self):
        text = "2026: value\n"
        assert yaml.safe_load(text) == {2026: "value"}
        assert mp.parse_yaml_minimal(text) == {"2026": "value"}
