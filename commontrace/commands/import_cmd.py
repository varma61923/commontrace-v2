from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import uuid

from commontrace import adapters, frontmatter, import_data, paths, templates, validate
from commontrace.commands import _validators
from commontrace.commands.capture_cmd import _id_suffix

_SLUGIFY_RE = re.compile(r"[^a-z0-9]+")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "import",
        help="Bulk-import an existing export (JSONL or CSV) into memory/traces/ -- "
        "'start from your historical traces' with no infrastructure replacement.",
    )
    p.add_argument("file", help="Path to a .jsonl or .csv export.")
    p.add_argument("--format", choices=["jsonl", "csv"], default=None, help="Default: infer from the file extension.")
    p.add_argument(
        "--source", choices=list(adapters.SOURCES), default=adapters.GENERIC,
        help=(
            "Which system produced this export. Anything but `generic` reads "
            "the vendor's own nested shape, so --title-field and friends are "
            "not needed (and are ignored): "
            + "; ".join(f"{a.name} = {a.describe}" for a in adapters.ADAPTERS.values()
                        if a.name != adapters.GENERIC)
            + ". These read a FILE you already have -- no API key, no network "
            "call, nothing leaves your machine."
        ),
    )
    p.add_argument(
        "--agent-type", type=_validators.agent_type, required=True,
        help="Kind of fleet these traces came from, as a lowercase slug. Any "
             "field works -- e.g. code, support, hr, robotics, legal.",
    )
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


_MAX_DETAILS = 20

_MAX_IMPORT_FILE_BYTES = 500 * 1024 * 1024


def run(args: argparse.Namespace) -> int:
    if not os.path.exists(args.file):
        print(f"[commontrace] no such file: {args.file}", file=sys.stderr)
        return 1
    if not os.path.isfile(args.file):
        print(f"[commontrace] not a regular file: {args.file}", file=sys.stderr)
        return 1
    try:
        if os.path.getsize(args.file) > _MAX_IMPORT_FILE_BYTES:
            print(
                f"[commontrace] {args.file} is larger than "
                f"{_MAX_IMPORT_FILE_BYTES // (1024 * 1024)} MiB; split the export "
                "or pass a smaller file",
                file=sys.stderr,
            )
            return 1
    except OSError as exc:
        print(f"[commontrace] cannot access {args.file}: {exc}", file=sys.stderr)
        return 1

    mapping = import_data.FieldMapping(
        title=args.title_field,
        context=args.context_field,
        solution=args.solution_field,
        tags=args.tags_field,
        id=args.id_field,
        source=getattr(args, "source", adapters.GENERIC),
    )
    fmt = _infer_format(args.file, args.format)

    if not args.dry_run:
        paths.warn_if_implicit_cwd_store(args.dest)
    root = paths.resolve_root(args.dest)
    tdir = paths.traces_dir(root)
    date = datetime.date.today().isoformat()
    schema = validate.load_schema("trace.schema.json")

    n_skipped = 0
    skip_samples: list[str] = []
    n_written = 0
    n_rejected = 0
    reject_samples: list[str] = []

    try:
        fh = open(args.file, "r", encoding="utf-8-sig", newline="" if fmt == "csv" else None)
    except OSError as exc:
        print(f"[commontrace] could not open {args.file}: {exc}", file=sys.stderr)
        return 1

    with fh:
        if not args.dry_run:
            os.makedirs(tdir, exist_ok=True)
        rows = import_data.iter_csv(fh, mapping) if fmt == "csv" else import_data.iter_jsonl(fh, mapping)
        for result in rows:
            if isinstance(result, import_data.SkippedRow):
                n_skipped += 1
                if len(skip_samples) < _MAX_DETAILS:
                    skip_samples.append(f"line {result.line_no}: {result.reason}")
                continue

            row = result
            trace_id = str(uuid.uuid4())

            fm = templates.trace_frontmatter(
                trace_id, row.title, args.agent_type, row.tags, args.profile, row.outcome or None
            )

            instance = dict(fm)
            instance["context_text"] = row.context_text
            instance["solution_text"] = row.solution_text
            errors = validate.validate(instance, schema)
            if errors:
                n_rejected += 1
                for err in errors:
                    if len(reject_samples) >= _MAX_DETAILS:
                        break
                    reject_samples.append(f"line {row.line_no}: {err}")
                continue

            if args.dry_run:
                n_written += 1
                continue

            slug = _slugify(row.title)
            out_path = os.path.join(tdir, f"{date}_{slug}_{_id_suffix(trace_id)}.md")

            body = templates.trace_body(row.context_text, row.solution_text)
            if row.source_id:
                safe_source_id = " ".join(str(row.source_id).split())
                body = f"<!-- imported from source id: {safe_source_id} -->\n" + body
            frontmatter.write(out_path, fm, body)
            n_written += 1

    n_parseable = n_written + n_rejected
    print(f"[commontrace] {args.file}: {n_parseable} row(s) parseable, {n_skipped} skipped.")
    for line in skip_samples:
        print(f"  [SKIP] {line}", file=sys.stderr)
    if n_skipped > len(skip_samples):
        print(f"  ... and {n_skipped - len(skip_samples)} more skipped row(s)", file=sys.stderr)

    all_rows_skipped = n_written == 0 and n_skipped > 0

    if n_rejected:
        verb = "would be rejected as schema-invalid" if args.dry_run else "rejected as schema-invalid and NOT written"
        print(f"[commontrace] {n_rejected} row(s) {verb}:", file=sys.stderr)
        for line in reject_samples:
            print(f"  [REJECT] {line}", file=sys.stderr)
        if n_rejected > _MAX_DETAILS:
            print(f"  ... and more (only the first {_MAX_DETAILS} are shown)", file=sys.stderr)

    if args.dry_run:
        print(f"[commontrace] --dry-run: no files written. {n_written} trace(s) would be created.")
        return 1 if (all_rows_skipped or n_rejected) else 0

    print(
        f"[commontrace] wrote {n_written} trace(s) to {tdir}. "
        "Run `commontrace distill` to find repeated patterns across them."
    )
    if n_rejected or all_rows_skipped:
        return 1
    return 0
