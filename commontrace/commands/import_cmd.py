from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import uuid

from commontrace import frontmatter, import_data, paths, templates, validate

_SLUGIFY_RE = re.compile(r"[^a-z0-9]+")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "import",
        help="Bulk-import an existing export (JSONL or CSV) into memory/traces/ -- "
        "'start from your historical traces' with no infrastructure replacement.",
    )
    p.add_argument("file", help="Path to a .jsonl or .csv export.")
    p.add_argument("--format", choices=["jsonl", "csv"], default=None, help="Default: infer from the file extension.")
    p.add_argument("--agent-type", choices=paths.AGENT_TYPES, required=True)
    p.add_argument("--profile", default="")
    p.add_argument("--title-field", default="title")
    p.add_argument("--context-field", default="context")
    p.add_argument("--solution-field", default="solution")
    p.add_argument("--tags-field", default="tags")
    p.add_argument("--id-field", default="id", help="Source system's own id, recorded for traceability only.")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Parse and report what would be imported, without writing any files.",
    )
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def _infer_format(path: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return "csv"
    return "jsonl"


def _slugify(title: str) -> str:
    slug = _SLUGIFY_RE.sub("-", title.lower()).strip("-")
    return slug[:60] or "trace"


def run(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.file):
        print(f"[commontrace] no such file: {args.file}", file=sys.stderr)
        return 1

    mapping = import_data.FieldMapping(
        title=args.title_field,
        context=args.context_field,
        solution=args.solution_field,
        tags=args.tags_field,
        id=args.id_field,
    )
    fmt = _infer_format(args.file, args.format)

    if fmt == "jsonl":
        with open(args.file, "r", encoding="utf-8") as fh:
            imported, skipped = import_data.parse_jsonl(fh, mapping)
    else:
        with open(args.file, "r", encoding="utf-8", newline="") as fh:
            imported, skipped = import_data.parse_csv(fh, mapping)

    print(f"[commontrace] {args.file}: {len(imported)} row(s) parseable, {len(skipped)} skipped.")
    for s in skipped[:20]:
        print(f"  [SKIP] line {s.line_no}: {s.reason}", file=sys.stderr)
    if len(skipped) > 20:
        print(f"  ... and {len(skipped) - 20} more skipped row(s)", file=sys.stderr)

    if args.dry_run:
        print(f"[commontrace] --dry-run: no files written. {len(imported)} trace(s) would be created.")
        return 0

    if not imported:
        return 0

    root = paths.resolve_root(args.dest)
    tdir = paths.traces_dir(root)
    os.makedirs(tdir, exist_ok=True)
    date = datetime.date.today().isoformat()

    written = 0
    rejected: list[tuple[int, list[str]]] = []
    schema = validate.load_schema("trace.schema.json")
    for row in imported:
        trace_id = str(uuid.uuid4())
        slug = _slugify(row.title)
        # Unconditionally id-suffixed, same reasoning as capture_cmd.py: the
        # `if os.path.exists()` fallback is check-then-act and loses a row
        # when two imports run at once, which is exactly what a migration
        # looks like when someone parallelizes it by splitting the file.
        out_path = os.path.join(tdir, f"{date}_{slug}_{trace_id[:8]}.md")

        fm = templates.trace_frontmatter(
            trace_id, row.title, args.agent_type, row.tags, args.profile, row.outcome or None
        )

        # Validate before writing, exactly as `capture` does. A bulk import is
        # the likeliest source of malformed records -- it is someone else's
        # export, not this tool's output -- so accepting what `capture` refuses
        # would make the importer the one hole in the store's invariants, and
        # a bad row would be averaged into `bench --pilot` until an audit ran.
        instance = dict(fm)
        instance["context_text"] = row.context_text
        instance["solution_text"] = row.solution_text
        errors = validate.validate(instance, schema)
        if errors:
            rejected.append((row.line_no, errors))
            continue

        body = templates.trace_body(row.context_text, row.solution_text)
        if row.source_id:
            body = f"<!-- imported from source id: {row.source_id} -->\n" + body
        # Atomic (NamedTemporaryFile + os.replace), same reasoning as
        # capture_cmd.py -- an import writes many files in a loop, so the
        # window in which a concurrent reader can see a torn file is not one
        # write long, it is the whole import.
        frontmatter.write(out_path, fm, body)
        written += 1

    print(
        f"[commontrace] wrote {written} trace(s) to {tdir}. "
        "Run `commontrace distill` to find repeated patterns across them."
    )
    if rejected:
        print(
            f"[commontrace] {len(rejected)} row(s) rejected as schema-invalid "
            "and NOT written:",
            file=sys.stderr,
        )
        for line_no, errors in rejected:
            for err in errors:
                print(f"  [REJECT] line {line_no}: {err}", file=sys.stderr)
        # Non-zero: a partial import that looks successful is how bad rows get
        # discovered a month later, in a report.
        return 1
    return 0
