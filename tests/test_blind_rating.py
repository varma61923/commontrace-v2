"""The blinded rating tool (commons/eval/blind_rating.py): blinding, refusal of partial data, arithmetic."""
import csv
import json
import random

import pytest

from commons.eval import blind_rating as br
from commontrace import frontmatter, templates


def _write(directory, n, prefix, llm=False):
    for i in range(n):
        fm = templates.lesson_frontmatter(
            slug=f"lesson_{prefix}_{i}", description=f"{prefix} {i}", agent_type="support", domain="d", tags=[],
            applies_when=f"when {prefix} {i} applies", do_not_apply_when=f"unless {prefix} {i} differs",
            importance=3, importance_rationale="r", source_traces=[], status="active")
        if llm:
            fm["llm_draft"] = {"provider": "anthropic", "model": "SECRET-MODEL-NAME"}
        body = f"## Rule\nDo {prefix} thing {i}.\n\n## Why\nbecause\n\n## How to apply\nx\n\n## Counter-examples\ny\n"
        frontmatter.write(str(directory / f"lesson_{prefix}_{i}.md"), fm, body)


@pytest.fixture
def dirs(tmp_path):
    a, b = tmp_path / "drafts", tmp_path / "hand"
    a.mkdir()
    b.mkdir()
    _write(a, 12, "drafted", llm=True)
    _write(b, 12, "written")
    return a, b


def test_the_sheet_says_nothing_about_where_an_item_came_from(dirs, tmp_path):
    out = tmp_path / "rating"
    assert br.main(["make", "--drafts", str(dirs[0]), "--handwritten", str(dirs[1]), "--n", "8",
                    "--out", str(out)]) == 0
    sheet = (out / "sheet.csv").read_text()
    for leak in ("SECRET-MODEL-NAME", "llm_draft", "anthropic", "lesson_drafted", "lesson_written"):
        assert leak not in sheet
    key = json.loads((out / "key.json").read_text())
    assert sorted(key.values()) == ["drafts"] * 8 + ["handwritten"] * 8
    rows = list(csv.DictReader(open(out / "sheet.csv", newline="")))
    assert [r["item_id"] for r in rows] == sorted(r["item_id"] for r in rows) and len(rows) == 16
    # shuffled: the first half is not simply one source
    assert len({key[r["item_id"]] for r in rows[:6]}) == 2 or len({key[r["item_id"]] for r in rows[:8]}) == 2


def test_the_same_seed_gives_the_same_draw_and_a_different_seed_does_not(dirs):
    d, h = br._read_lessons(str(dirs[0])), br._read_lessons(str(dirs[1]))
    a = br.make(d, h, 6, 1)
    assert a[:2] == br.make(d, h, 6, 1)[:2] and a[1] != br.make(d, h, 6, 2)[1]


def _ratings(key, drafts_mean, hand_mean, seed=0, raters=("r1",)):
    rng = random.Random(seed)
    rows = []
    for item, source in key.items():
        for r in raters:
            mean = drafts_mean if source == "drafts" else hand_mean
            rows.append({"item_id": item, "rater": r,
                         **{c: str(min(5, max(1, round(rng.gauss(mean, 0.6))))) for c in br.CRITERIA}})
    return rows


KEY = {f"item-{i:03d}": ("drafts" if i % 2 else "handwritten") for i in range(100)}


def test_equal_quality_is_not_called_inferior_and_clearly_better_drafts_are_superior():
    same = br.score(_ratings(KEY, 3.5, 3.5), KEY)
    assert same["verdict"] in ("NON-INFERIOR", "INCONCLUSIVE") and abs(same["criteria"]["overall"]["difference"]) < 0.4
    better = br.score(_ratings(KEY, 4.4, 3.2), KEY)
    assert better["verdict"] == "SUPERIOR" and better["criteria"]["overall"]["ci_low"] > 0


def test_clearly_worse_drafts_are_inferior():
    worse = br.score(_ratings(KEY, 2.5, 4.2), KEY)
    assert worse["verdict"] == "INFERIOR" and worse["criteria"]["overall"]["ci_high"] < 0


def test_a_looser_margin_changes_the_verdict_and_the_margin_is_reported():
    ratings = _ratings(KEY, 3.3, 3.6, seed=3)
    tight, loose = br.score(ratings, KEY, margin=0.05), br.score(ratings, KEY, margin=0.6)
    assert tight["margin"] == 0.05 and loose["margin"] == 0.6
    assert loose["verdict"] == "NON-INFERIOR" and tight["verdict"] != "NON-INFERIOR"


@pytest.mark.parametrize("mutate,message", [
    (lambda rows: rows[1:], "no rating"),
    (lambda rows: [{**rows[0], "item_id": "item-999"}, *rows[1:]], "not in the key"),
    (lambda rows: [{**rows[0], "correct": "9"}, *rows[1:]], "1 to 5"),
    (lambda rows: [{**rows[0], "correct": ""}, *rows[1:]], "non-numeric"),
])
def test_partial_or_invalid_ratings_are_refused_not_scored(mutate, message):
    with pytest.raises(ValueError, match=message):
        br.score(mutate(_ratings(KEY, 3.5, 3.5)), KEY)


def test_several_raters_are_averaged_per_item_and_their_agreement_is_reported():
    result = br.score(_ratings(KEY, 4.0, 3.0, raters=("a", "b", "c")), KEY)
    assert result["raters"] == 3 and 0 < result["agreement_within_one_point"] <= 1


def test_the_length_cue_is_reported(dirs, tmp_path):
    d, h = br._read_lessons(str(dirs[0])), br._read_lessons(str(dirs[1]))
    h = [{**x, "rule": x["rule"] * 6} for x in h]
    assert "differ in length" in br.make(d, h, 6, 0)[2]["length_cue"]
