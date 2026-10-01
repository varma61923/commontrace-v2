"""Blinded rating of model-drafted lessons against hand-written ones.

    python -m commons.eval.blind_rating make  --drafts DIR --handwritten DIR --n 50 --out rating/
    python -m commons.eval.blind_rating score --ratings rating/ratings.csv --key rating/key.json

`make` draws N lessons from each directory (a model's drafts and lessons people wrote), strips everything
that says where one came from (the llm_draft provenance, the slug and file name, evidence sections, any
authorship), renders each as the same four fields in the same layout, shuffles them, and writes:

    sheet.csv   one row per item: item_id, the text, and empty score columns. This is the only file raters see.
    key.json    item_id -> source. Keep it away from raters until every rating is in.

Raters score each item 1 to 5 on four criteria and may rate on their own copy; put every rater's rows in one
`ratings.csv` with a `rater` column. `score` refuses to run on missing items or ratings, then reports each
source's mean per criterion, the difference (drafts minus hand-written) with a bootstrap 95% interval over
items, and one verdict: SUPERIOR if the whole interval is above zero, NON-INFERIOR if it is above -margin,
otherwise INFERIOR (or INCONCLUSIVE when the interval spans both a meaningful loss and a gain). The margin is
a choice made before looking (default 0.25 points on the 5-point scale) and printed with the result.

Blinding is as good as the text: a draft that reads like a model (or a hand-written lesson that reads like a
person) can be told apart by a rater. `make` reports the one cue it can check, whether the two sources differ in
length by more than 25%, and says so; it does not rewrite either.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import statistics
import sys

CRITERIA = ("correct", "specific", "actionable", "safe_to_inject")
DEFAULT_MARGIN = 0.25
BOOTSTRAP = 4000


def _read_lessons(directory: str) -> list[dict]:
    from commontrace import frontmatter

    out = []
    for name in sorted(os.listdir(directory)):
        if not name.startswith("lesson_") or not name.endswith(".md") or name == "lesson_template.md":
            continue
        fm, body = frontmatter.read(os.path.join(directory, name))
        rule = _section(body, "Rule")
        out.append({"rule": rule, "applies_when": str(fm.get("applies_when", "")).strip(),
                    "do_not_apply_when": str(fm.get("do_not_apply_when", "")).strip()})
    return [x for x in out if x["rule"]]


def _section(body: str, name: str) -> str:
    import re

    m = re.search(rf"^##\s*{re.escape(name)}\s*\n(.*?)(?=\n##\s|\Z)", body, re.S | re.M | re.I)
    return m.group(1).strip() if m else ""


def render(item: dict) -> str:
    """The one layout every item is shown in. Nothing here can identify its source."""
    return (f"RULE: {item['rule']}\nAPPLIES WHEN: {item['applies_when']}\n"
            f"DOES NOT APPLY WHEN: {item['do_not_apply_when']}")


def make(drafts: list[dict], handwritten: list[dict], n: int, seed: int) -> tuple[list[dict], dict, dict]:
    """(sheet rows, key, report). Samples min(n, available) from each source."""
    rng = random.Random(f"blind:{seed}")
    take = min(n, len(drafts), len(handwritten))
    if take == 0:
        raise ValueError("both sources need at least one lesson")
    picked = ([("drafts", x) for x in rng.sample(drafts, take)]
              + [("handwritten", x) for x in rng.sample(handwritten, take)])
    rng.shuffle(picked)
    rows, key = [], {}
    for i, (source, item) in enumerate(picked, start=1):
        item_id = f"item-{i:03d}"
        rows.append({"item_id": item_id, "text": render(item), **{c: "" for c in CRITERIA}})
        key[item_id] = source
    lengths = {s: statistics.fmean(len(render(it)) for src, it in picked if src == s)
               for s in ("drafts", "handwritten")}
    ratio = max(lengths.values()) / max(1.0, min(lengths.values()))
    report = {"items_per_source": take, "mean_length": lengths,
              "length_cue": "the sources differ in length by more than 25%; raters may be able to tell them apart"
              if ratio > 1.25 else "lengths are within 25% of each other"}
    return rows, key, report


def _bootstrap_diff(a: list[float], b: list[float], seed: int) -> tuple[float, float, float]:
    rng = random.Random(f"boot:{seed}")
    diffs = []
    for _ in range(BOOTSTRAP):
        sa = [a[rng.randrange(len(a))] for _ in a]
        sb = [b[rng.randrange(len(b))] for _ in b]
        diffs.append(statistics.fmean(sa) - statistics.fmean(sb))
    diffs.sort()
    return statistics.fmean(a) - statistics.fmean(b), diffs[int(0.025 * BOOTSTRAP)], diffs[int(0.975 * BOOTSTRAP) - 1]


def score(ratings: list[dict], key: dict, margin: float = DEFAULT_MARGIN, seed: int = 0) -> dict:
    """Combine ratings and key. Refuses an incomplete or mismatched set rather than scoring part of it."""
    by_item: dict[str, dict[str, list[float]]] = {}
    raters = set()
    for row in ratings:
        item = row.get("item_id")
        if item not in key:
            raise ValueError(f"rating for {item!r}, which is not in the key")
        raters.add(row.get("rater", ""))
        for c in CRITERIA:
            try:
                value = float(row[c])
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"{item}: missing or non-numeric {c!r}") from None
            if not 1 <= value <= 5:
                raise ValueError(f"{item}: {c} must be 1 to 5, got {value:g}")
            by_item.setdefault(item, {}).setdefault(c, []).append(value)
    missing = sorted(set(key) - set(by_item))
    if missing:
        raise ValueError(f"{len(missing)} item(s) have no rating, e.g. {missing[0]}")
    per_item = {item: statistics.fmean(statistics.fmean(v) for v in crit.values()) for item, crit in by_item.items()}
    out: dict = {"raters": len(raters), "margin": margin, "criteria": {}}
    for c in (*CRITERIA, "overall"):
        values = {src: [(statistics.fmean(by_item[i][c]) if c != "overall" else per_item[i])
                        for i in key if key[i] == src] for src in ("drafts", "handwritten")}
        diff, low, high = _bootstrap_diff(values["drafts"], values["handwritten"], seed)
        out["criteria"][c] = {"drafts": statistics.fmean(values["drafts"]), "handwritten":
                              statistics.fmean(values["handwritten"]), "difference": diff, "ci_low": low,
                              "ci_high": high, "n_per_source": len(values["drafts"])}
    o = out["criteria"]["overall"]
    out["verdict"] = ("SUPERIOR" if o["ci_low"] > 0 else "NON-INFERIOR" if o["ci_low"] > -margin
                      else "INFERIOR" if o["ci_high"] < 0 else "INCONCLUSIVE")
    if len(raters) > 1:
        close = []
        for item, crit in by_item.items():
            for values in crit.values():
                close.append(max(values) - min(values) <= 1)
        out["agreement_within_one_point"] = sum(close) / len(close)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make")
    mk.add_argument("--drafts", required=True)
    mk.add_argument("--handwritten", required=True)
    mk.add_argument("--n", type=int, default=50)
    mk.add_argument("--seed", type=int, default=0)
    mk.add_argument("--out", required=True)
    sc = sub.add_parser("score")
    sc.add_argument("--ratings", required=True)
    sc.add_argument("--key", required=True)
    sc.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    sc.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "make":
            rows, key, report = make(_read_lessons(args.drafts), _read_lessons(args.handwritten), args.n, args.seed)
            os.makedirs(args.out, exist_ok=True)
            with open(os.path.join(args.out, "sheet.csv"), "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=["item_id", "text", *CRITERIA])
                w.writeheader()
                w.writerows(rows)
            with open(os.path.join(args.out, "key.json"), "w", encoding="utf-8") as fh:
                json.dump(key, fh, indent=2)
            print(f"wrote {len(rows)} items to {args.out}/sheet.csv (give raters this) and key.json (keep it away "
                  f"from them). {report['length_cue']}.")
            return 0
        with open(args.ratings, encoding="utf-8", newline="") as fh:
            ratings = list(csv.DictReader(fh))
        with open(args.key, encoding="utf-8") as fh:
            key = json.load(fh)
        result = score(ratings, key, args.margin)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{result['raters']} rater(s); margin {result['margin']} points. Drafts minus hand-written:")
        for c, v in result["criteria"].items():
            print(f"  {c:15s} drafts {v['drafts']:.2f}  hand-written {v['handwritten']:.2f}  "
                  f"diff {v['difference']:+.2f} [{v['ci_low']:+.2f}, {v['ci_high']:+.2f}]")
        print(f"verdict: {result['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
