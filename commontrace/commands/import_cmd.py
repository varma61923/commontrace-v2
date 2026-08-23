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


# Only the first N skip/reject reasons are held in memory and printed;
# beyond that only a count is kept. Unbounded lists of full reasons was the
# other half of the memory issue this streams around -- a large export with
# many bad rows would otherwise accumulate a string per row indefinitely.
_MAX_DETAILS = 20


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

    root = paths.resolve_root(args.dest)
    tdir = paths.traces_dir(root)
    date = datetime.date.today().isoformat()
    schema = validate.load_schema("trace.schema.json")

    if not args.dry_run:
        os.makedirs(tdir, exist_ok=True)

    # Single pass over the file, streamed via iter_jsonl/iter_csv rather than
    # collecting parse_jsonl/parse_csv's full result lists: a multi-gigabyte
    # export was previously read entirely into memory -- as parsed Python
    # objects for every row, not just raw bytes -- before a single trace file
    # was written, which OOM-kills a memory-constrained container before it
    # reports anything at all. Everything below is bounded: counts grow
    # without limit, but the lists of human-readable detail lines do not.
    n_skipped = 0
    skip_samples: list[str] = []
    n_written = 0
    n_rejected = 0
    reject_samples: list[str] = []

    # utf-8-sig, not plain utf-8: see commontrace/failure_import.py's
    # read_failures for why -- a BOM-prefixed CSV/JSONL export (Excel,
    # Windows tools) otherwise lands a literal U+FEFF in the first header
    # cell or JSON key. Identical to utf-8 for files without a BOM.
    with open(args.file, "r", encoding="utf-8-sig", newline="" if fmt == "csv" else None) as fh:
        rows = import_data.iter_csv(fh, mapping) if fmt == "csv" else import_data.iter_jsonl(fh, mapping)
        for result in rows:
            if isinstance(result, import_data.SkippedRow):
                n_skipped += 1
                if len(skip_samples) < _MAX_DETAILS:
                    skip_samples.append(f"line {result.line_no}: {result.reason}")
                continue

            row = result
            if args.dry_run:
                n_written += 1  # counts "would be written" in dry-run mode
                continue

            trace_id = str(uuid.uuid4())
            slug = _slugify(row.title)
            # Unconditionally id-suffixed, same reasoning as capture_cmd.py:
            # the `if os.path.exists()` fallback is check-then-act and loses
            # a row when two imports run at once, which is exactly what a
            # migration looks like when someone parallelizes it by splitting
            # the file.
            out_path = os.path.join(tdir, f"{date}_{slug}_{trace_id[:8]}.md")

            fm = templates.trace_frontmatter(
                trace_id, row.title, args.agent_type, row.tags, args.profile, row.outcome or None
            )

            # Validate before writing, exactly as `capture` does. A bulk
            # import is the likeliest source of malformed records -- it is
            # someone else's export, not this tool's output -- so accepting
            # what `capture` refuses would make the importer the one hole in
            # the store's invariants, and a bad row would be averaged into
            # `bench --pilot` until an audit ran.
            instance = dict(fm)
            instance["context_text"] = row.context_text
            instance["solution_text"] = row.solution_text
            errors = validate.validate(instance, schema)
            if errors:
                n_rejected += 1
                if len(reject_samples) < _MAX_DETAILS:
                    for err in errors:
                        reject_samples.append(f"line {row.line_no}: {err}")
                continue

            body = templates.trace_body(row.context_text, row.solution_text)
            if row.source_id:
                # Collapse ALL whitespace (including embedded newlines) to
                # single spaces before embedding: trace_io._first_wins finds
                # "## Context"/"## Solution" by matching `^##...` at the
                # START OF A LINE (re.MULTILINE), and this HTML comment is
                # plain text to that regex, not a real comment boundary. An
                # unsanitized source_id containing "...\n## Context\nfake\n"
                # would inject a same-named section BEFORE the real one, and
                # "first occurrence wins" means the fake one -- not this
                # row's actual imported content -- is what every downstream
                # reader (bench, query, lesson promotion) sees. A source_id
                # can never legitimately need an embedded newline; the
                # source system's id is a single token or short string.
                safe_source_id = " ".join(str(row.source_id).split())
                body = f"<!-- imported from source id: {safe_source_id} -->\n" + body
            # Atomic (NamedTemporaryFile + os.replace), same reasoning as
            # capture_cmd.py -- an import writes many files in a loop, so the
            # window in which a concurrent reader can see a torn file is not
            # one write long, it is the whole import.
            frontmatter.write(out_path, fm, body)
            n_written += 1

    n_parseable = n_written + n_rejected
    print(f"[commontrace] {args.file}: {n_parseable} row(s) parseable, {n_skipped} skipped.")
    for line in skip_samples:
        print(f"  [SKIP] {line}", file=sys.stderr)
    if n_skipped > len(skip_samples):
        print(f"  ... and {n_skipped - len(skip_samples)} more skipped row(s)", file=sys.stderr)

    if args.dry_run:
        print(f"[commontrace] --dry-run: no files written. {n_written} trace(s) would be created.")
        return 0

    print(
        f"[commontrace] wrote {n_written} trace(s) to {tdir}. "
        "Run `commontrace distill` to find repeated patterns across them."
    )
    if n_rejected:
        print(
            f"[commontrace] {n_rejected} row(s) rejected as schema-invalid and NOT written:",
            file=sys.stderr,
        )
        for line in reject_samples:
            print(f"  [REJECT] {line}", file=sys.stderr)
        if n_rejected > _MAX_DETAILS:
            print(f"  ... and more (only the first {_MAX_DETAILS} are shown)", file=sys.stderr)
        # Non-zero: a partial import that looks successful is how bad rows get
        # discovered a month later, in a report.
        return 1
    return 0
