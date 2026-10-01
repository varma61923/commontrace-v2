"""`commontrace bill`: invoice proven value from verified proof packages.

Nothing here has a default price. `bill template` prints the terms a schedule must set, all empty; the owner
fills them in. `bill invoice` previews by default and writes the billing book only with `--commit`, so looking
at what an invoice would be changes nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from commontrace import pricing, proof


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "bill", help="Invoice proven value from verified proof packages (no default prices; preview unless --commit).")
    sub = p.add_subparsers(dest="bill_cmd", required=True)
    tp = sub.add_parser("template", help="Print the commercial terms a price schedule must set, all empty.")
    tp.set_defaults(func=run_template)
    inv = sub.add_parser("invoice", help="Build one invoice from a schedule and proof packages.")
    inv.add_argument("--schedule", required=True, help="Your price schedule (see `bill template`).")
    inv.add_argument("--org", required=True)
    inv.add_argument("--period", required=True, help="A label billed once, e.g. 2026-Q1.")
    inv.add_argument("--agent-months", type=float, required=True,
                     help="Agents under management, summed over the months of the period.")
    inv.add_argument("--package", action="append", default=[], metavar="DIR",
                     help="A proof package directory (repeatable).")
    inv.add_argument("--key-file", default=None, help="The issuer's key, to check each package's signature.")
    inv.add_argument("--book", default=".", help="Directory holding billing.json (what was charged before).")
    inv.add_argument("--commit", action="store_true", help="Record the invoice in the book. Default: preview only.")
    inv.add_argument("--json", action="store_true")
    inv.set_defaults(func=run_invoice)


def run_template(_args: argparse.Namespace) -> int:
    print(json.dumps({k: None for k in pricing._FIELDS}, indent=2))
    print("\n# Every term is required; none has a default. value_basis is one of: "
          + ", ".join(pricing.VALUE_BASES) + ". cap_per_invoice may be null for no cap.", file=sys.stderr)
    return 0


def run_invoice(args: argparse.Namespace) -> int:
    try:
        schedule = pricing.PriceSchedule.from_file(args.schedule)
        key = proof.read_key(args.key_file) if args.key_file else None
        book = pricing.read_book(args.book)
        inv = pricing.invoice(book, schedule, org=args.org, period=args.period, agent_months=args.agent_months,
                              packages=args.package, key=key, commit=args.commit)
        if args.commit:
            pricing.write_book(args.book, book)
            with open(os.path.join(args.book, f"invoice-{args.org}-{args.period}.json"), "w", encoding="utf-8") as fh:
                json.dump(inv.to_dict(), fh, indent=2)
    except (pricing.PricingError, proof.ProofError, ValueError) as exc:
        print(f"[commontrace] error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(inv.to_dict(), indent=2) if args.json else pricing.render(inv))
    if not args.commit:
        print("\n(preview: nothing was recorded; add --commit to record this invoice)", file=sys.stderr)
    return 0
