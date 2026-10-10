"""`commontrace market`: list, verify and install lessons that carry replicated proof of lift."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import marketplace, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "market",
        help="Lesson marketplace: signed listings backed by replicated randomized lift; installs land in review.",
    )
    sub = p.add_subparsers(dest="market_cmd", required=True)

    ident = sub.add_parser("identity", help="Create or show this store's Ed25519 identity for a role, "
                                            "and print the public card to share.")
    ident.add_argument("--role", choices=sorted(marketplace.ROLES), required=True)
    ident.add_argument("--id", default="", help="Principal id (only when creating).")
    ident.add_argument("--organization", default="", help="Organization (only when creating).")
    ident.set_defaults(func=run_identity)

    tr = sub.add_parser("trust", help="Trust a public card (from `market identity`) for a role. Operator decision.")
    tr.add_argument("card", help="Path to the card JSON.")
    tr.add_argument("--role", choices=sorted(marketplace.ROLES), required=True)
    tr.set_defaults(func=run_trust)

    at = sub.add_parser("attest", help="Fleet: sign the lift this store's randomized holdout measured for a lesson.")
    at.add_argument("slug")
    at.add_argument("--min-arm", type=int, default=20, help="Resolved occasions required in each arm.")
    at.add_argument("--out", required=True)
    at.set_defaults(func=run_attest)

    ce = sub.add_parser("certify", help="Referee: pool independent fleet receipts into a signed certificate.")
    ce.add_argument("receipts", nargs="+")
    ce.add_argument("--out", required=True)
    ce.set_defaults(func=run_certify)

    pu = sub.add_parser("publish", help="Publisher: sign a listing for an active, certified lesson.")
    pu.add_argument("slug")
    pu.add_argument("--certificate", required=True)
    pu.add_argument("--licence-id", required=True, help="An SPDX id or your own terms id.")
    pu.add_argument("--terms", required=True, help="Licence terms text.")
    pu.add_argument("--price-usd", type=float, default=None)
    pu.add_argument("--per", choices=("install", "month", "year"), default="install")
    pu.add_argument("--outcome-share", type=float, default=None,
                    help="Fraction (up to 0.5) of each buyer's own proven value from this lesson.")
    pu.add_argument("--out", default=None, help="Also write the listing JSON here.")
    pu.set_defaults(func=run_publish)

    ve = sub.add_parser("verify", help="Check a listing against the keys this store trusts.")
    ve.add_argument("listing")
    ve.add_argument("--json", action="store_true")
    ve.set_defaults(func=run_verify)

    ins = sub.add_parser("install", help="Verify a listing and install its lesson at status=review.")
    ins.add_argument("listing")
    ins.add_argument("--accept-licence", required=True, help="The listing's licence id, after reading its terms.")
    ins.set_defaults(func=run_install)

    st = sub.add_parser("settle", help="What this store owes publishers for a period: licence fees plus "
                                       "outcome shares on lift proven in this store's own holdout.")
    st.add_argument("--period", required=True, help="YYYY-MM")
    st.add_argument("--value-per-occasion", type=float, default=None,
                    help="What one improved occasion is worth to you (needed for outcome shares).")
    st.add_argument("--commit", action="store_true", help="Record the statement; a period settles once.")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=run_settle)

    ls = sub.add_parser("list", help="Listings in this store's catalog.")
    ls.add_argument("--query", default="")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=run_list)

    for parser in (ident, tr, at, ce, pu, ve, ins, st, ls):
        parser.add_argument("--dest", default=None)


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write(path: str, value: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, sort_keys=True)


def _guarded(fn):
    def run(args: argparse.Namespace) -> int:
        try:
            return fn(args, paths.resolve_root(args.dest))
        except (marketplace.MarketError, OSError, ValueError) as exc:
            print(f"[commontrace] market: {exc}", file=sys.stderr)
            return 1
    return run


@_guarded
def run_identity(args, root) -> int:
    create = bool(args.id or args.organization)
    principal = marketplace.identity(root, args.role, principal_id=args.id, organization=args.organization,
                                     create=create)
    print(json.dumps(marketplace.card(principal), indent=2, sort_keys=True))
    return 0


@_guarded
def run_trust(args, root) -> int:
    entry = marketplace.trust(root, _read(args.card), role=args.role)
    print(f"[commontrace] trusting {args.role} {entry['id']} ({entry['organization']}).")
    return 0


@_guarded
def run_attest(args, root) -> int:
    measured = marketplace.measured_effect(root, args.slug, min_arm=args.min_arm)
    receipt = marketplace.attest(root, args.slug, effect=measured)
    _write(args.out, receipt)
    print(f"[commontrace] attested lift {measured['effect']:+.3f} (se {measured['standard_error']:.3f}, "
          f"{measured['n_injected']} injected / {measured['n_withheld']} withheld) -> {args.out}")
    return 0


@_guarded
def run_certify(args, root) -> int:
    certificate = marketplace.certify(root, [_read(p) for p in args.receipts])
    pooled = certificate["record"]["pooled"]
    _write(args.out, certificate)
    print(f"[commontrace] pooled lift {pooled['effect']:+.3f} [{pooled['ci_low']:+.3f}, {pooled['ci_high']:+.3f}] "
          f"across {pooled['organizations']} organizations -> {args.out}")
    return 0


@_guarded
def run_publish(args, root) -> int:
    licence = {"id": args.licence_id, "terms": args.terms}
    if args.price_usd is not None:
        licence["price"] = {"amount_usd": args.price_usd, "per": args.per}
    if args.outcome_share is not None:
        licence["outcome_share"] = args.outcome_share
    listing = marketplace.publish(root, args.slug, _read(args.certificate), licence)
    if args.out:
        _write(args.out, listing)
    print(f"[commontrace] listed {args.slug} as {marketplace.listing_id(listing)[:12]}"
          + (f" -> {args.out}" if args.out else ""))
    return 0


@_guarded
def run_verify(args, root) -> int:
    report = marketplace.verify(_read(args.listing), marketplace.load_trust(root))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif report["ok"]:
        lift = report["lift"]
        print(f"[commontrace] verified: lift {lift['effect']:+.3f} [{lift['ci_low']:+.3f}, {lift['ci_high']:+.3f}] "
              f"across {lift['organizations']} organizations; licence {report['licence']['id']}.")
    else:
        print("[commontrace] NOT verified:\n  - " + "\n  - ".join(report["problems"]), file=sys.stderr)
    return 0 if report["ok"] else 1


@_guarded
def run_install(args, root) -> int:
    row = marketplace.install(root, _read(args.listing), accept_licence=args.accept_licence)
    if row.get("already_installed"):
        print(f"[commontrace] already installed as {row['slug']}.")
    else:
        print(f"[commontrace] installed as {row['slug']} at status=review. Its certified lift was measured "
              f"elsewhere; review it, then `commontrace lesson approve {row['slug']}` and let this store's "
              "holdout measure it again.")
    return 0


@_guarded
def run_settle(args, root) -> int:
    from commontrace import market_settlement

    try:
        statement = market_settlement.settle(root, args.period, value_per_occasion=args.value_per_occasion,
                                             commit=args.commit)
    except market_settlement.SettlementError as exc:
        raise marketplace.MarketError(str(exc)) from None
    if args.json:
        print(json.dumps(statement, indent=2, sort_keys=True))
        return 0
    print(f"[commontrace] marketplace statement for {args.period}"
          + ("" if args.commit else " (preview; --commit records it)") + ":")
    for line in statement["lines"]:
        print(f"  {line['publisher']:<20} {line['kind']:<14} {line['slug']:<28} ${line['amount_usd']:.2f}")
    for item in statement["refused"]:
        print(f"  no outcome share for {item['slug']}: {item['reason']}")
    print(f"  total ${statement['total_usd']:.2f}  (digest {statement['digest'][:12]})")
    return 0


@_guarded
def run_list(args, root) -> int:
    rows = [marketplace.summary(x) for x in marketplace.catalog(root, args.query)]
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    if not rows:
        print("[commontrace] no listings in this catalog.")
    for r in rows:
        price = f" ${r['price']['amount_usd']:g}/{r['price']['per']}" if r.get("price") else ""
        print(f"{r['id'][:12]}  {r['name']}  lift {r['lift']:+.3f} ({r['organizations']} orgs)  "
              f"{r['licence']}{price}  by {r['publisher']}")
    return 0
