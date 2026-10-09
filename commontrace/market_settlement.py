"""What a buyer owes marketplace publishers for a period, from licences and proven lift.

Two kinds of charge, both from terms the publisher signed into the listing:

* **Fees** (``price``): ``per: install`` is owed once, in the period of install;
  ``per: month`` accrues each period the lesson is installed and not rejected;
  ``per: year`` accrues one twelfth per period.
* **Outcome share** (``outcome_share``): a fraction of the value the lesson
  proved *in the buyer's own randomized holdout*. Nothing accrues until the
  buyer's anytime-valid verdict for that lesson is HELPS; the basis is the
  conservative lower bound ``ci_low x injected occasions x value_per_occasion``,
  and each period bills only the increase over the basis already settled, so a
  lesson is never paid twice for the same proof. Lift measured elsewhere (the
  listing's certificate) never earns a share: it is what justified trying the
  lesson, not proof that it helps here.

Statements are hash-chained in ``memory/market/settlements.json``; a period is
settled once.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re

from commontrace import _jsonl, frontmatter, lesson_io, paths

_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class SettlementError(ValueError):
    pass


def _book_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "market", "settlements.json")


def _read_book(root: str) -> dict:
    try:
        with open(_book_path(root), encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"schema": "commontrace.market-settlements.v1", "statements": [], "outcome_basis": {}}


def _lesson_effects(root: str) -> dict:
    from commontrace import experiment
    from commontrace.commands import experiment_cmd

    rows, _rate, _corrupt = experiment_cmd._load(root)
    rows, _salt, _other = experiment_cmd.scope_to_current_salt(root, rows)
    obs = experiment_cmd._observations(rows)
    effects = experiment.analyze(obs, sequential=True) if obs else []
    return {lesson_io.canonical_slug(e.lesson_slug): e for e in effects}


def _status(root: str, slug: str) -> str | None:
    path = lesson_io.lesson_path(root, slug)
    if path is None:
        return None
    return frontmatter.read(path)[0].get("status")


def settle(root: str, period: str, *, value_per_occasion: float | None = None, commit: bool = False) -> dict:
    """The statement for `period` (YYYY-MM). With commit=True it is recorded and cannot be issued again."""
    from commontrace import marketplace

    if not _PERIOD.match(period):
        raise SettlementError("period must be YYYY-MM")
    if value_per_occasion is not None and not value_per_occasion >= 0:
        raise SettlementError("value_per_occasion must be non-negative")
    book = _read_book(root)
    if any(s["period"] == period for s in book["statements"]):
        raise SettlementError(f"{period} is already settled")
    effects = _lesson_effects(root)
    lines, refused = [], []
    new_basis = dict(book.get("outcome_basis", {}))
    for row in marketplace.installed(root):
        slug, licence, publisher = row["slug"], row["licence"], row["publisher"]
        status = _status(root, slug)
        price = licence.get("price")
        if price and status not in (None, "rejected"):
            per, amount = price["per"], price["amount_usd"]
            due = (amount if per == "month" else amount / 12 if per == "year"
                   else amount if row["installed_at"][:7] == period else 0.0)
            if due:
                lines.append({"publisher": publisher, "listing": row["listing"], "slug": slug, "kind": f"fee-{per}",
                              "amount_usd": round(due, 2), "licence": licence["id"]})
        share = licence.get("outcome_share")
        if not share:
            continue
        if value_per_occasion is None:
            refused.append({"slug": slug, "reason": "outcome share needs --value-per-occasion"})
            continue
        effect = effects.get(lesson_io.canonical_slug(slug))
        if status != "active" or effect is None or effect.verdict != "HELPS":
            refused.append({"slug": slug, "reason": "no HELPS verdict in this store's own holdout yet"
                            if status == "active" else f"the lesson is {status or 'missing'}, not active"})
            continue
        basis = max(0.0, effect.ci_low) * effect.n_injected * value_per_occasion
        previous = float(book.get("outcome_basis", {}).get(row["listing"], 0.0))
        delta = basis - previous
        if delta <= 0:
            continue
        new_basis[row["listing"]] = basis
        lines.append({"publisher": publisher, "listing": row["listing"], "slug": slug, "kind": "outcome-share",
                      "amount_usd": round(delta * share, 2), "licence": licence["id"],
                      "evidence": {"verdict": effect.verdict, "ci_low": effect.ci_low, "effect": effect.effect,
                                   "n_injected": effect.n_injected, "n_withheld": effect.n_withheld,
                                   "value_per_occasion": value_per_occasion, "basis_usd": round(basis, 2),
                                   "previously_settled_basis_usd": round(previous, 2), "share": share}})
    totals: dict[str, float] = {}
    for line in lines:
        totals[line["publisher"]] = round(totals.get(line["publisher"], 0.0) + line["amount_usd"], 2)
    previous_digest = book["statements"][-1]["digest"] if book["statements"] else None
    statement = {"schema": "commontrace.market-settlement.v1", "period": period, "lines": lines,
                 "refused": refused, "totals_usd": totals, "total_usd": round(sum(totals.values()), 2),
                 "previous_digest": previous_digest,
                 "issued_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    statement["digest"] = hashlib.sha256(json.dumps(statement, sort_keys=True).encode("utf-8")).hexdigest()
    if commit:
        path = _book_path(root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _jsonl.locked(path):
            current = _read_book(root)
            if any(s["period"] == period for s in current["statements"]):
                raise SettlementError(f"{period} is already settled")
            current["statements"].append(statement)
            current["outcome_basis"] = new_basis
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(current, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
    return statement
