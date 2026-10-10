from __future__ import annotations

import argparse
import glob
import importlib.util
import os
import re
import shutil
import sys

from commontrace import frontmatter, paths, store_state
from commontrace.commands._shellout import find_reference_script


def _installed(module: str) -> bool:
    if module in sys.modules:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:  # noqa: BLE001 - see above; a probe must not be fatal
        return False


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("doctor", help="Check the environment and store health.")
    p.add_argument("--dest", default=None)
    p.add_argument("--troubleshooting", action="store_true",
                   help="Print what each check means and how to fix it, as Markdown, and exit.")
    p.set_defaults(func=run)


TROUBLESHOOTING: dict[str, tuple[str, str]] = {
    "Python >= 3.10": ("The package uses syntax and library features from Python 3.10.",
                       "Install Python 3.10 or newer and reinstall: `python3 -m pip install commontrace`."),
    "PyYAML importable": ("Lessons and traces are Markdown with YAML frontmatter, read with PyYAML.",
                          "`python3 -m pip install 'PyYAML>=6,<7'` in the same environment as `commontrace`."),
    "OpenTelemetry tracing": ("Tracing was turned on (COMMONTRACE_OTEL or OTEL_EXPORTER_OTLP_ENDPOINT) but spans "
                              "cannot be exported.",
                              "`python3 -m pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http`, "
                              "or unset COMMONTRACE_OTEL."),
    "OAuth resource server": ("Remote clients may authenticate with JWT access tokens from your authorization server "
                              "(COMMONTRACE_OAUTH_ISSUER / _AUDIENCE / _JWKS_URL or _JWKS_FILE).",
                              "Set both issuer and audience and exactly one JWKS source (https, or a file), and "
                              "`python3 -m pip install 'commontrace[security]'`; unset COMMONTRACE_OAUTH_ISSUER "
                              "to turn OAuth off."),
    "git on PATH": ("Some commands read the repository's history.", "Install git, or ignore this if you do not use "
                                                                    "those commands."),
    "memory/ store present": ("Every command reads a store: a `memory/` directory.",
                              "Run `commontrace init --function <name>` here, or pass `--dest` / set "
                              "`COMMONTRACE_ROOT` to the directory that holds `memory/`."),
    "lessons in store": ("With no lessons there is nothing to retrieve or measure.",
                         "Capture experience (`commontrace capture`), then `commontrace distill`, review and "
                         "`commontrace lesson approve`; or `commontrace kb install` for a curated pack."),
    "retrieval ready (>= 1 ACTIVE lesson)": ("The store has content but nothing active, so `query` returns nothing.",
                                             "Approve a candidate: `commontrace lesson list --status review`, then "
                                             "`commontrace lesson approve <slug>`."),
    "trace filename collisions": ("Two traces whose file names collide overwrite each other.",
                                  "Re-capture the affected traces; recent versions suffix every name with an id."),
    "expired lessons": ("A lesson past its `expires` date is hidden from retrieval, so its guidance silently stops.",
                        "Renew the date if the lesson still holds, or archive it."),
    "atomic facts": ("Facts feed agent context; forgotten and expired ones are kept for audit but not shown.",
                     "`commontrace fact list --include-forgotten` to review them."),
    "credentials in stored traces": ("A trace holding a credential would be replayed to every later reader.",
                                     "Run `commontrace redact` on the store and rotate the credential."),
    "store agent_type": ("The declared kind of agent decides starter domains and defaults.",
                         "Set it with `commontrace init --agent-type <type>`; any value is valid."),
    "attention extra (numpy + sentence-transformers)": ("Semantic ranking needs numpy and sentence-transformers; "
                                                        "without them retrieval is lexical.",
                                                        "`python3 -m pip install 'commontrace[attention]'`, then "
                                                        "`commontrace index`."),
    "MCP SDK (agent-native access via `commontrace serve`)": ("`commontrace serve` needs the MCP SDK.",
                                                              "`python3 -m pip install 'commontrace[serve]'`."),
    "reference attention/query.py": ("The semantic query script ships inside the package.",
                                     "`python3 -m pip install --force-reinstall commontrace`."),
    "warm query worker": ("Without it every semantic query loads the embedding model again (seconds, not "
                          "milliseconds).",
                          "Make the runtime directory private: `chmod 700` the directory named above (or set "
                          "XDG_RUNTIME_DIR to one that is), and make sure it is owned by you."),
    "benchmark script found": ("`commontrace bench` runs a script that ships inside the package.",
                               "`python3 -m pip install --force-reinstall commontrace`."),
    "pilot metrics script found": ("`commontrace pilot` runs a script that ships inside the package.",
                                   "`python3 -m pip install --force-reinstall commontrace`."),
    "protocol/ spec": ("The spec is in a repository checkout; an installed client carries the schemas instead.",
                       "Nothing to do for an installed client."),
    "retrieval fusion": ("Reports how lesson scores are combined.", "Change it with `commontrace retrieval`."),
    "retrieval reranking": ("Reports whether a reranker is configured.", "Change it with `commontrace retrieval`."),
    "{} model cached": ("A semantic model that is not downloaded is fetched on first use, which can stall a "
                        "first query or fail offline.",
                        "Run `commontrace index` once with network access, or point the model setting at a local "
                        "directory."),
    "{} model": ("Reports which embedding model a role uses.", "Change it with `commontrace retrieval`."),
    "gateway token file protected": ("The gateway's bearer token lets whoever holds it read and write this store.",
                                     "`chmod 600 memory/gateway.token`; rotate it by deleting the file and "
                                     "restarting the gateway."),
    "experiment outcomes reported": ("A held-out memory without outcomes cannot be measured; a run that never "
                                     "reports them is not an experiment.",
                                     "Report each occasion's result under the same occasion id (`POST /v1/outcome` "
                                     "or `CausalMemory.record_outcome`); see `commontrace proof status`."),
    "experiment integrity": ("A COMPROMISED experiment (for example, two rates under one salt) states no effect.",
                             "Read `commontrace proof status` for the named mechanism; start a fresh "
                             "randomization (change the salt) rather than pooling."),
}


def troubleshooting_markdown() -> str:
    lines = ["# Troubleshooting", "", "Generated from the checks `commontrace doctor` runs.", ""]
    for label, (why, fix) in TROUBLESHOOTING.items():
        lines += [f"## {label.replace('{}', '<name>')}", "", why, "", f"**Fix:** {fix}", ""]
    return "\n".join(lines)


_FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "", critical: bool = False) -> None:
    mark = "OK  " if ok else "WARN"
    line = f"[{mark}] {label}"
    if detail:
        line += f" - {detail}"
    print(line)
    if not ok:
        entry = TROUBLESHOOTING.get(label)
        if entry:
            print(f"       fix: {entry[1]}")
    if not ok and critical:
        _FAILURES.append(label)


def _info(label: str, detail: str = "") -> None:
    line = "[INFO] " + label
    if detail:
        line += f" - {detail}"
    print(line)


def _legacy_suffix(trace_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", trace_id).strip("-.")
    return safe[:16]


def _collision_suspects(root: str) -> int:
    from commontrace import holdout_io

    try:
        records, _ = holdout_io.read_log(root)
    except Exception:  # noqa: BLE001 - doctor must not fail on a damaged log
        return 0
    if not records:
        return 0

    on_disk: set[str] = set()
    for path in glob.glob(os.path.join(paths.traces_dir(root), "*.md")):
        if os.path.basename(path) == "README.md":
            continue
        try:
            fm, _ = frontmatter.read(path)
        except Exception:  # noqa: BLE001
            continue
        on_disk.add(str(fm.get("id", "")))

    groups: dict[str, set[str]] = {}
    for rec in records:
        groups.setdefault(_legacy_suffix(rec.occasion_id), set()).add(rec.occasion_id)

    suspects = 0
    for ids in groups.values():
        if len(ids) < 2:
            continue
        present = {i for i in ids if i in on_disk}
        if present and len(present) < len(ids):
            suspects += 1
    return suspects


def _declared_agent_type(root: str) -> str | None:
    try:
        with open(paths.index_path(root), encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return None
    _, sep, value = first.partition("agent_type:")
    if not sep:
        return None
    return value.strip() or None


def _traces_with_credentials(root: str) -> int:
    from commontrace import memory_guard
    from commontrace.commands.redact_cmd import trace_paths

    count = 0
    for path in trace_paths(root):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                if memory_guard.redact_secrets(fh.read())[1]:
                    count += 1
        except OSError:
            continue
    return count


def _models_in_use(root: str, config) -> list[tuple[str, str]]:
    from commontrace import rerank_arm, retrieval_io, semantic_arm

    models = []
    if config.fusion != retrieval_io.FUSION_NONE:
        models.append(("embedding", semantic_arm.stored_model(root) or _default_embedder()))
    if config.rerank != retrieval_io.RERANK_NONE and config.rerank in rerank_arm.MODELS:
        models.append(("reranker", rerank_arm.MODELS[config.rerank][0]))
    return models


def _default_embedder() -> str:
    from commontrace.semantic_arm import _load_reference

    builder = _load_reference("", os.path.join("memory", "attention", "build_index.py"),
                              "commontrace_reference_build_index_doctor")
    return getattr(builder, "DEFAULT_MODEL_NAME", "") if builder else ""


def _model_cached(name: str) -> bool:
    if not name:
        return False
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception:  # noqa: BLE001 - cannot tell: say nothing reassuring
        return False
    for repo in (name,) if "/" in name else (name, f"sentence-transformers/{name}"):
        if isinstance(try_to_load_from_cache(repo, "config.json"), str):
            return True
    return False


def _report_memory_lifecycle(root: str) -> None:
    from commontrace import frontmatter, hierarchical, lesson_cache, ttl

    try:
        lessons = lesson_cache.load_active(root, None, reader=frontmatter.read)
        facts = list(hierarchical.load_facts(root).values())
    except (OSError, ValueError):
        return
    counts = ttl.summarize(lessons, facts)
    if counts["expired_lessons"]:
        _info("expired lessons", f"{counts['expired_lessons']} active lesson(s) past `expires` are hidden "
                                 "from retrieval; renew or archive them")
    if counts["total_facts"]:
        _info("atomic facts", f"{counts['total_facts']} stored, {counts['forgotten_facts']} forgotten, "
                              f"{counts['expired_facts']} past `expires_at`")


def _check_measurement(root: str) -> None:
    import stat

    from commontrace import gateway, holdout_io

    token = gateway.token_path(root)
    if os.path.isfile(token) and os.name == "posix":
        mode = stat.S_IMODE(os.stat(token).st_mode)
        _check("gateway token file protected", mode & 0o077 == 0, f"mode {oct(mode)}")
    config = holdout_io.load_config(root)
    if not (config.started_at and config.running):
        return
    rows, _corrupt = holdout_io.read_log(root)
    if not rows:
        return
    occasions = {r.occasion_id for r in rows}
    answered = set(holdout_io.read_outcomes(root))
    share = len(occasions & answered) / len(occasions)
    _check("experiment outcomes reported", share >= 0.5 or len(occasions) < 20,
           f"{share:.0%} of {len(occasions)} occasions have an outcome")
    from commontrace import integrity
    from commontrace.commands import experiment_cmd

    try:
        assignments, _rate, _bad = experiment_cmd._load(root)
        assignments, _salt, _other = experiment_cmd.scope_to_current_salt(root, assignments)
        verdict = integrity.audit(assignments).verdict
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not take the report down
        _check("experiment integrity", False, f"could not audit: {exc}")
        return
    _check("experiment integrity", verdict != integrity.VERDICT_COMPROMISED, verdict)


def run(args: argparse.Namespace) -> int:
    if getattr(args, "troubleshooting", False):
        print(troubleshooting_markdown())
        return 0
    _FAILURES.clear()
    root = paths.resolve_root(args.dest)
    print(f"[commontrace] doctor - store root: {root}\n")

    _check("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0], critical=True)
    _check("PyYAML importable", _installed("yaml"), critical=True)
    _check("git on PATH", shutil.which("git") is not None)
    from commontrace import telemetry

    tele = telemetry.status()
    if tele["otel_enabled"]:
        _check("OpenTelemetry tracing", tele["tracing"] and not tele["note"],
               tele["note"] or f"exporting to {tele['endpoint'] or 'the default OTLP endpoint'}")
    else:
        print("  [info] telemetry: metrics in-process (gateway /v1/metrics); set COMMONTRACE_OTEL=1 or "
              "OTEL_EXPORTER_OTLP_ENDPOINT to export traces")
    from commontrace import oauth, offline

    try:
        oauth_config = oauth.load_config(root)
    except Exception as exc:  # noqa: BLE001 - doctor reports, it never raises
        _check("OAuth resource server", False, f"configuration error: {exc}")
    else:
        if oauth_config is not None:
            _check("OAuth resource server", True,
                   f"issuer {oauth_config.issuer}, audience {oauth_config.audience}, "
                   f"metadata at {oauth.metadata_url(oauth_config)}")
    if offline.enabled():
        print("  [info] offline mode: non-loopback network calls are refused and models load from the "
              "local cache only (COMMONTRACE_OFFLINE)")

    has_mem = os.path.isdir(paths.memory_dir(root))
    _check("memory/ store present", has_mem,
           paths.memory_dir(root) if has_mem else "run `commontrace init`", critical=True)

    if has_mem:
        n_lessons = 0
        ldir = paths.lessons_dir(root)
        if os.path.isdir(ldir):
            try:
                n_lessons = len(
                    [
                        f
                        for f in os.listdir(ldir)
                        if f.startswith("lesson_") and f != "lesson_template.md"
                    ]
                )
            except OSError:
                n_lessons = 0
        _check("lessons in store", n_lessons > 0, f"{n_lessons} found")

        state = store_state.inspect(root)
        stuck = state.active == 0 and (state.traces > 0 or state.lessons > 0)
        if stuck:
            _check(
                "retrieval ready (>= 1 ACTIVE lesson)",
                False,
                f"{state.active} active, {state.review} at review, {state.traces} trace(s)",
            )
            print()
            print(store_state.why_no_results(root, searched="query"))
            print()
        elif state.active:
            _check(
                "retrieval ready (>= 1 ACTIVE lesson)",
                True,
                f"{state.active} active, {state.review} at review, {state.traces} trace(s)",
            )

        collided = _collision_suspects(root)
        if collided:
            _check(
                "trace filename collisions", False,
                f"{collided} trace file(s) hold fewer captures than were made under "
                "them. Before this was fixed, two occasion ids sharing their first 16 "
                "characters (e.g. TICKET-PROJECT-4711 and -4712) wrote to one filename "
                "and the second silently replaced the first. New captures are safe; "
                "these are already-lost traces. Re-capture them if the source data "
                "still exists.",
            )
        else:
            _check("trace filename collisions", True, "none detected")

        leaked = _traces_with_credentials(root)
        if leaked:
            _check(
                "credentials in stored traces", False,
                f"{leaked} trace file(s) hold an API key, token or private key captured before "
                "traces were redacted on write. `commontrace redact` removes them "
                "(`--dry-run` to preview); then rotate those keys.",
            )
        else:
            _check("credentials in stored traces", True, "none found")

        _report_memory_lifecycle(root)

        declared = _declared_agent_type(root)
        effective = paths.store_agent_type(root)
        if declared is None:
            _info(
                "store agent_type",
                f"not declared in memory/INDEX.md; commands will assume '{effective}'. "
                "Add `agent_type: <your fleet>` to the first line to make it explicit.",
            )
        elif declared == effective:
            _check("store agent_type", True, f"{declared!r} (any field is valid; taxonomy is open)")
        else:
            _check(
                "store agent_type", False,
                f"memory/INDEX.md declares {declared!r} but commands read back "
                f"{effective!r} -- it is not a valid slug "
                f"({paths.AGENT_TYPE_RE.pattern}). Traces are being stamped "
                f"{effective!r}. Fix the first line of memory/INDEX.md.",
            )

    attention_extra = _installed("numpy") and _installed("sentence_transformers")
    if attention_extra:
        _check("attention extra (numpy + sentence-transformers)", True, "installed")
        from commontrace import retrieval_io

        config = retrieval_io.load_config(root)
        if config.fusion == retrieval_io.FUSION_NONE:
            _info(
                "retrieval fusion",
                "off -- the attention extra is installed, so keyword + meaning ranking "
                "is available: `commontrace retrieval --fusion gated --rerank "
                "cross-encoder` (starts a new randomization if an experiment is running)",
            )
        if config.rerank == retrieval_io.RERANK_NONE:
            _info(
                "retrieval reranking",
                "off -- the attention extra is installed, so a cross-encoder can "
                "reorder the top candidates: `commontrace retrieval --rerank "
                "cross-encoder` (starts a new randomization if an experiment is running)",
            )
        for role, name in _models_in_use(root, config):
            if _model_cached(name):
                _check(f"{role} model cached", True, name)
            else:
                _info(
                    f"{role} model",
                    f"{name} is not in the local model cache: the first query downloads it "
                    "(needs internet, a few hundred MB). On a host without internet, copy "
                    "~/.cache/huggingface/ from a machine that has run a query.",
                )
    else:
        _info(
            "attention extra (numpy + sentence-transformers)",
            "not installed; optional -- `pip install commontrace[attention]` for semantic retrieval",
        )

    if _installed("mcp"):
        _check("MCP SDK (agent-native access via `commontrace serve`)", True, "installed")
    else:
        _info(
            "MCP SDK (agent-native access via `commontrace serve`)",
            "not installed; optional -- `pip install commontrace[serve]` to let agents "
            "that cannot run a shell use this store",
        )

    query_script = find_reference_script(root, "memory/attention/query.py")
    if query_script is not None:
        _check("reference attention/query.py", True, query_script)
        if attention_extra:
            from commontrace import warm

            healthy, detail = warm.status(query_script)
            if healthy:
                _info("warm query worker", detail)
            else:
                _check("warm query worker", False, detail)
    else:
        _check("reference attention/query.py", False,
               "missing from the installed package - try `pip install --force-reinstall commontrace`")

    bench_script = find_reference_script(root, "benchmark/measure_performance.py")
    if bench_script is not None:
        _check("benchmark script found", True, bench_script)
    else:
        _check("benchmark script found", False,
               "missing from the installed package - try `pip install --force-reinstall commontrace`")

    pilot_script = find_reference_script(root, "benchmark/pilot_metrics.py")
    if pilot_script is not None:
        _check("pilot metrics script found", True, pilot_script)
    else:
        _check("pilot metrics script found", False,
               "missing from the installed package - try `pip install --force-reinstall commontrace`")

    protocol_dir = os.path.join(root, "protocol")
    if os.path.isdir(protocol_dir):
        _check("protocol/ spec", True, protocol_dir)
    else:
        _info(
            "protocol/ spec",
            "not present; expected for a pip-installed client -- schemas are mirrored "
            "at commontrace/schemas/",
        )

    if has_mem:
        _check_measurement(root)

    if _FAILURES:
        print(f"\nDone. {len(_FAILURES)} critical check(s) failed: "
              + ", ".join(_FAILURES))
        return 1

    print("\nDone.")
    return 0
