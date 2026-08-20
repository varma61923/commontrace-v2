from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from commontrace import frontmatter, overlap, paths, trace_io


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "overlap",
        help="Measure how much another fleet's lessons would help yours, "
        "exchanging signatures instead of content.",
    )
    sub = p.add_subparsers(dest="overlap_cmd", required=True)

    sign = sub.add_parser(
        "sign",
        help="Reduce this store's lessons + recurring failures to a signature file. "
        "Lesson/trace text is not included; slugs and tags are, unless redacted.",
    )
    sign.add_argument("--fleet-label", required=True, help="A name for your fleet, e.g. 'acme-support'.")
    sign.add_argument("--out", required=True, help="Where to write the signature JSON.")
    sign.add_argument("--num-perm", type=int, default=overlap.DEFAULT_NUM_PERM)
    sign.add_argument(
        "--redact-labels", action="store_true",
        help="Replace lesson slugs / trace ids with salted hashes. Use when the other "
        "fleet should not learn what your lessons are *about* -- a slug like "
        "'lesson_stripe_idempotency' describes itself. You can map the hashes back "
        "locally; the report stays usable, just less readable.",
    )
    sign.add_argument(
        "--redact-tags", action="store_true",
        help="Also drop tags and domains. Makes the report's per-domain breakdown "
        "empty, but reveals nothing about which technologies your fleet uses.",
    )
    sign.add_argument("--dest", default=None)
    sign.set_defaults(func=run_sign)

    rep = sub.add_parser(
        "report",
        help="Compare two signature files: what would --ours gain from --theirs?",
    )
    rep.add_argument("--ours", required=True, help="Signature file for the fleet that would benefit.")
    rep.add_argument("--theirs", required=True, help="Signature file for the fleet providing lessons.")
    rep.add_argument("--threshold", type=float, default=overlap.DEFAULT_MATCH_THRESHOLD)
    rep.add_argument("--json", action="store_true", help="Emit raw JSON instead of markdown.")
    rep.set_defaults(func=run_report)


def _iter_lessons(root: str):
    ldir = paths.lessons_dir(root)
    for path in sorted(glob.glob(os.path.join(ldir, "lesson_*.md"))):
        if os.path.basename(path) == "lesson_template.md":
            continue
        fm, _ = frontmatter.read(path)
        if fm.get("status") != "active":
            continue
        yield fm


def _iter_recurring_failures(root: str):
    """A recurring failure is a trace with outcome.repeated_error = true --
    the fleet hit the same thing twice. Those are exactly the failures a
    commons could have prevented, which is why the report counts them and
    not every trace."""
    tdir = paths.traces_dir(root)
    for path in sorted(glob.glob(os.path.join(tdir, "*.md"))):
        if os.path.basename(path) == "README.md":
            continue
        instance, _ = trace_io.read(path)
        if (instance.get("outcome") or {}).get("repeated_error") is True:
            yield instance


def _label(raw: str, args: argparse.Namespace) -> str:
    return overlap.redact_label(raw) if args.redact_labels else raw


def run_sign(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    sig = overlap.FleetSignature(fleet_label=args.fleet_label, num_perm=args.num_perm)

    for fm in _iter_lessons(root):
        # Sign the activation condition, not the rule text: the question is
        # "does this lesson apply to that situation", which is what
        # applies_when describes.
        text = " ".join(
            str(x) for x in (
                fm.get("applies_when", ""), fm.get("description", ""),
                fm.get("domain", ""), " ".join(fm.get("tags") or []),
            )
        )
        sig.items.append(
            overlap.SignedItem(
                label=_label(str(fm.get("name", "")), args), kind="lesson",
                domain="" if args.redact_tags else str(fm.get("domain", "")),
                tags=[] if args.redact_tags else list(fm.get("tags") or []),
                signature=overlap.minhash(text, args.num_perm),
            )
        )

    for tr in _iter_recurring_failures(root):
        text = " ".join(
            str(x) for x in (
                tr.get("title", ""), tr.get("context_text", ""),
                " ".join(tr.get("tags") or []),
            )
        )
        sig.items.append(
            overlap.SignedItem(
                label=_label(str(tr.get("id", ""))[:12], args), kind="failure",
                domain="", tags=[] if args.redact_tags else list(tr.get("tags") or []),
                signature=overlap.minhash(text, args.num_perm),
            )
        )

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(sig.to_dict(), fh, indent=2)

    n_lessons = len(sig.lessons())
    n_failures = len(sig.failures())
    print(f"[commontrace] wrote {args.out}")
    print(f"  {n_lessons} lesson(s) + {n_failures} recurring failure(s), as signatures only.")
    if n_failures == 0:
        print(
            "  NOTE: no recurring failures found (traces with outcome.repeated_error=true).\n"
            "  Capture them with `commontrace capture --repeated-error`, or import a\n"
            "  'repeated_error' column, for the report to have anything to measure.",
            file=sys.stderr,
        )
    print("\n  What this file contains:")
    print("    - MinHash signatures (lesson/trace TEXT is not recoverable from these)")
    label_note = "redacted hashes" if args.redact_labels else (
        "REAL slugs/ids (e.g. lesson_stripe_idempotency)"
    )
    print(f"    - labels: {label_note}")
    print(f"    - tags/domains: {'omitted' if args.redact_tags else 'INCLUDED (reveals which technologies you use)'}")
    if not (args.redact_labels and args.redact_tags):
        print(
            "  Review it before sending: slugs and tags are human-written and can be\n"
            "  descriptive. Re-run with --redact-labels/--redact-tags to strip them.",
            file=sys.stderr,
        )
    return 0


def run_report(args: argparse.Namespace) -> int:
    for path in (args.ours, args.theirs):
        if not os.path.isfile(path):
            print(f"[commontrace] no such signature file: {path}", file=sys.stderr)
            return 1

    with open(args.ours, encoding="utf-8") as fh:
        ours = overlap.FleetSignature.from_dict(json.load(fh))
    with open(args.theirs, encoding="utf-8") as fh:
        theirs = overlap.FleetSignature.from_dict(json.load(fh))

    try:
        report = overlap.build_report(ours, theirs, threshold=args.threshold)
    except ValueError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    if args.json:
        import dataclasses

        print(json.dumps(dataclasses.asdict(report), indent=2))
    else:
        print(overlap.render_report(report))
    return 0
