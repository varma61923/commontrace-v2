"""UniMem JSON workflows, grouped under existing command themes."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unimem import frontier_protocol as fp
from unimem import paths, pooled_io


def request(value: str) -> dict[str, Any]:
    if value.lstrip().startswith("{"):
        raw = value.encode()
    else:
        with Path(value).open("rb") as stream:
            raw = stream.read(fp.MAX_BYTES + 1)
    if len(raw) > fp.MAX_BYTES:
        raise ValueError("UniMem request exceeds 1 MiB")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("UniMem request must be a JSON object")
    fp.wire(result)
    return result


def add_experiment(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="unimem_experiment")
    personal = commands.add_parser("sycophancy", help="UniMem matched-query outcome, accuracy and agreement audit.")
    personal.add_argument("request", nargs="?", default="{}", help="UniMem JSON request with action and fields.")
    personal.add_argument("--dest")
    personal.set_defaults(func=run_sycophancy)
    export = commands.add_parser("export-rl", help="Stream UniMem governed randomized-arm RL records as JSONL.")
    export.add_argument("request", nargs="?", default="{}", help="UniMem JSON object or request file.")
    export.add_argument("--format", choices=("dpo", "advantage", "replay"), default="dpo")
    export.add_argument("--dest")
    export.set_defaults(func=run_rl)
    pool = commands.add_parser("pool", help="UniMem signed evidence pooling across configured ownership groups.")
    operations = pool.add_subparsers(dest="unimem_pool", required=True)
    for operation in ("identity", "register", "privacy", "contribute", "accept", "report"):
        child = operations.add_parser(operation, help="UniMem pooled evidence: " + operation + ".")
        child.add_argument("request", nargs="?", default="{}", help="UniMem JSON object or request file.")
        child.add_argument("--dest")
        child.set_defaults(func=run_pool)


def run_sycophancy(args: argparse.Namespace) -> int:
    from unimem import sycophancy_io

    body = request(args.request)
    action = body.pop("action", "report")
    operations: dict[str, Callable[..., Any]] = {"register": sycophancy_io.register,
        "expose": sycophancy_io.expose, "report": sycophancy_io.report, "benchmark": sycophancy_io.benchmark}
    if action not in operations:
        raise ValueError("UniMem personalization action must be register, expose, report or benchmark")
    try:
        result = operations[action](paths.resolve_root(args.dest), **body)
    except TypeError as exc:
        raise ValueError("UniMem personalization fields do not match the requested action") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def run_pool(args: argparse.Namespace) -> int:
    body = request(args.request)
    root = paths.resolve_root(args.dest)
    operations: dict[str, Callable[..., Any]] = {"identity": pooled_io.issue_identity, "register": pooled_io.register,
                  "privacy": pooled_io.configure_privacy, "contribute": pooled_io.contribute,
                  "accept": pooled_io.accept, "report": pooled_io.report}
    try:
        result = operations[args.unimem_pool](root, **body)
    except TypeError as exc:
        raise ValueError("UniMem pooled request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def run_rl(args: argparse.Namespace) -> int:
    import sys

    from unimem import rl_io

    body = request(args.request)
    if "format" in body:
        raise ValueError("UniMem RL format belongs in the --format option")
    root = paths.resolve_root(args.dest)
    manifest = rl_io.prepare(root, format=args.format, **body)
    for line in rl_io.lines(root, manifest["id"], context=body.get("context", [])):
        sys.stdout.write(line.decode("utf-8"))
    return 0


def add_skill(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("skill", help="UniMem measured skill induction, trials and publishing.")
    operations = parser.add_subparsers(dest="unimem_skill", required=True)
    for operation in ("induce", "screen", "trial", "status", "publish", "retire"):
        child = operations.add_parser(operation, help="UniMem causal skill workflow: " + operation + ".")
        child.add_argument("request", nargs="?", default="{}", help="UniMem JSON object or request file.")
        child.add_argument("--dest")
        child.set_defaults(func=run_skill)

    optimize = operations.add_parser("optimize", help="UniMem randomized evidence for optimizer proposals.")
    actions = optimize.add_subparsers(dest="unimem_optimize", required=True)
    for action in ("intake", "status", "frontier"):
        child = actions.add_parser(action, help="UniMem optimizer workflow: " + action + ".")
        child.add_argument("request", nargs="?", default="{}", help="UniMem JSON object or request file.")
        child.add_argument("--dest")
        child.set_defaults(func=run_optimizer)


def run_optimizer(args: argparse.Namespace) -> int:
    from unimem import optimizer_io

    operations: dict[str, Callable[..., Any]] = {"intake": optimizer_io.intake, "status": optimizer_io.status,
                                               "frontier": optimizer_io.frontier}
    try:
        result = operations[args.unimem_optimize](paths.resolve_root(args.dest), **request(args.request))
    except TypeError as exc:
        raise ValueError("UniMem optimizer request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def run_skill(args: argparse.Namespace) -> int:
    from unimem import skill_io

    body = request(args.request)
    root = paths.resolve_root(args.dest)
    operations: dict[str, Callable[..., Any]] = {"induce": skill_io.induce, "screen": skill_io.screen,
        "trial": skill_io.trial, "status": skill_io.status, "publish": skill_io.publish, "retire": skill_io.retire}
    try:
        result = operations[args.unimem_skill](root, **body)
    except TypeError as exc:
        raise ValueError("UniMem skill request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def add_team(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("team", help="UniMem governed team memory and writer trials.")
    operations = parser.add_subparsers(dest="unimem_team", required=True)
    for operation in ("writers", "register-writer", "reliability", "mask", "mast", "route", "register-capability-agent",
                      "grant", "attenuate", "revoke", "handoff", "isolation", "lease", "read"):
        child = operations.add_parser(operation, help="UniMem team workflow: " + operation + ".")
        child.add_argument("request", nargs="?", default="{}", help="UniMem JSON object or request file.")
        child.add_argument("--dest")
        child.set_defaults(func=run_team)


def run_team(args: argparse.Namespace) -> int:
    from unimem import capability_io, team_io, writer_holdout

    operations: dict[str, Callable[..., Any]] = {"writers": writer_holdout.writers,
        "register-writer": writer_holdout.register, "reliability": writer_holdout.reliability,
        "mask": writer_holdout.mask, "mast": writer_holdout.tag_mast, "route": writer_holdout.route,
        "register-capability-agent": capability_io.register_agent, "grant": capability_io.grant,
        "attenuate": capability_io.attenuate, "revoke": capability_io.revoke, "handoff": capability_io.handoff,
        "isolation": team_io.isolation, "lease": team_io.lease, "read": team_io.fetch}
    body = request(args.request)
    try:
        result = operations[args.unimem_team](paths.resolve_root(args.dest), **body)
    except TypeError as exc:
        raise ValueError("UniMem team request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def add_sentinel(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("sentinel", help="UniMem prospective transfer quarantine and forgetting ledger.")
    operations = parser.add_subparsers(dest="unimem_sentinel", required=True)
    for operation in ("configure", "register", "status", "quarantine", "ledger"):
        child = operations.add_parser(operation, help="UniMem sentinel workflow: " + operation + ".")
        child.add_argument("request", nargs="?", default="{}", help="UniMem JSON object or request file.")
        child.add_argument("--dest")
        child.set_defaults(func=run_sentinel)


def run_sentinel(args: argparse.Namespace) -> int:
    from unimem import sentinel_io

    operations: dict[str, Callable[..., Any]] = {"configure": sentinel_io.configure, "register": sentinel_io.register,
        "status": sentinel_io.status, "quarantine": sentinel_io.quarantine, "ledger": sentinel_io.ledger}
    try:
        result = operations[args.unimem_sentinel](paths.resolve_root(args.dest), **request(args.request))
    except TypeError as exc:
        raise ValueError("UniMem sentinel request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


COMMANDS = {"skill": add_skill, "team": add_team, "sentinel": add_sentinel}


def add_stream(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("stream", help="UniMem admitted changes and downstream erasure acknowledgements.")
    operations = parser.add_subparsers(dest="unimem_stream", required=True)
    for operation in ("tail", "subscribe", "acks"):
        child = operations.add_parser(operation, help="UniMem memory stream: " + operation + ".")
        child.add_argument("request", nargs="?", default="{}", help="UniMem JSON object or request file.")
        child.add_argument("--dest")
        child.set_defaults(func=run_stream)


def run_stream(args: argparse.Namespace) -> int:
    from unimem import change_stream

    body = request(args.request)
    root = paths.resolve_root(args.dest)
    operations: dict[str, Callable[..., Any]] = {"tail": change_stream.poll, "subscribe": change_stream.subscribe,
        "acks": change_stream.acknowledge if "receipt_digest" in body else change_stream.obligations}
    try:
        result = operations[args.unimem_stream](root, **body)
    except TypeError as exc:
        raise ValueError("UniMem stream request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def add_erasure(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("erasure", help="UniMem bounded raw-store deletion verification.")
    operations = parser.add_subparsers(dest="unimem_erasure", required=True)
    child = operations.add_parser("verify", help="UniMem local byte scan and acknowledged erasure receipt.")
    child.add_argument("request", help="UniMem JSON object or request file; fingerprint strings stay in RAM.")
    child.add_argument("--dest")
    child.set_defaults(func=run_erasure)


def run_erasure(args: argparse.Namespace) -> int:
    from unimem import erasure_verify

    try:
        result = erasure_verify.verify(paths.resolve_root(args.dest), **request(args.request))
    except TypeError as exc:
        raise ValueError("UniMem erasure request fields do not match verification") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result.get("receipt") else 1


COMMANDS.update({"stream": add_stream, "erasure": add_erasure})


def add_tier(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("tier",
        help="UniMem managed payload residency, hibernation and embedding generations.")
    operations = parser.add_subparsers(dest="unimem_tier", required=True)
    for operation in ("status", "hibernate", "warm", "residency", "reembed", "rollback"):
        child = operations.add_parser(operation, help="UniMem namespace tier operation: " + operation + ".")
        child.add_argument("request", help="UniMem JSON object or request file.")
        child.add_argument("--dest")
        child.set_defaults(func=run_tier)

    capsules = operations.add_parser("capsules", help="Build, inspect or evict governed per-memory model KV.")
    actions = capsules.add_subparsers(dest="unimem_capsule", required=True)
    for action in ("build", "status", "evict"):
        child = actions.add_parser(action)
        child.add_argument("request", help="UniMem JSON request or request file.")
        child.add_argument("--dest")
        child.set_defaults(func=run_capsules)


def run_tier(args: argparse.Namespace) -> int:
    from unimem import tiering

    operations: dict[str, Callable[..., Any]] = {"status": tiering.status, "hibernate": tiering.hibernate,
        "warm": tiering.warm, "residency": tiering.configure, "reembed": tiering.reembed, "rollback": tiering.rollback}
    try:
        result = operations[args.unimem_tier](paths.resolve_root(args.dest), **request(args.request))
    except TypeError as exc:
        raise ValueError("UniMem tier request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


COMMANDS["tier"] = add_tier


def run_capsules(args: argparse.Namespace) -> int:
    from unimem import kv_capsules

    handlers: dict[str, Callable[..., Any]] = {"build": kv_capsules.build, "status": kv_capsules.status,
                                                "evict": kv_capsules.evict}
    try:
        result = handlers[args.unimem_capsule](paths.resolve_root(args.dest), **request(args.request))
    except TypeError as exc:
        raise ValueError("UniMem capsule request fields do not match the action") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def add_person(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("person", help="Own, review, export and selectively disclose personal memory.")
    operations = parser.add_subparsers(dest="unimem_person", required=True)
    for operation in ("identity", "trust", "export", "import", "stage", "configure-credentials",
                      "issue", "revoke", "challenge", "present", "holder-key", "verify", "status",
                      "purposes", "consent", "explain", "withdraw", "reap"):
        child = operations.add_parser(operation)
        child.add_argument("request", nargs="?", default="{}", help="JSON request object or request file.")
        child.add_argument("--dest")
        if operation == "stage":
            child.add_argument("--file", help="Read an assistant summary into review; never auto-admit it.")
        child.set_defaults(func=run_person)

    policy = operations.add_parser("policy", help="Compile and inspect regulated-subject write rules.")
    actions = policy.add_subparsers(dest="unimem_subject_policy", required=True)
    for operation in ("compile", "test", "explain"):
        child = actions.add_parser(operation)
        child.add_argument("request", nargs="?", default="{}")
        child.add_argument("--dest")
        child.set_defaults(func=run_person)

    household = operations.add_parser("household", help="Manage consent-scoped household memory and no-memory rooms.")
    household.add_argument("action", choices=("consent", "room", "scope"))
    household.add_argument("request", nargs="?", default="{}", help="JSON request object or request file.")
    household.add_argument("--dest")
    household.set_defaults(func=run_person)


def run_person(args: argparse.Namespace) -> int:
    from unimem import household, person_io, purpose, subject_policy

    body = request(args.request)
    if args.unimem_person == "stage" and getattr(args, "file", None):
        if "text" in body:
            raise ValueError("UniMem choose either summary text or --file")
        with Path(args.file).open("rb") as stream:
            raw = stream.read(128*1024+1)
        if len(raw) > 128*1024:
            raise ValueError("UniMem inbound summary exceeds 128 KiB")
        body["text"] = raw.decode("utf-8")
        body.setdefault("source", "file:"+Path(args.file).name)
    operations: dict[str, Callable[..., Any]] = {"identity": person_io.identity, "trust": person_io.trust,
        "export": person_io.export, "import": person_io.import_passport, "stage": person_io.stage,
        "configure-credentials": person_io.configure_credentials, "issue": person_io.issue,
        "revoke": person_io.revoke, "challenge": person_io.challenge, "present": person_io.present,
        "verify": person_io.verify_credential, "status": person_io.status, "holder-key": person_io.holder_key,
        "purposes": purpose.purposes, "consent": purpose.consent, "explain": purpose.explain,
        "withdraw": purpose.withdraw, "reap": purpose.reap}
    try:
        if args.unimem_person == "household":
            handlers: dict[str, Callable[..., Any]] = {"consent": household.consent, "room": household.configure_room,
                        "scope": lambda root, **fields: {"scope": household.scope_for(root, fields["person_id"])} }
            result = handlers[args.action](paths.resolve_root(args.dest), **body)
            print(json.dumps(result, indent=2, allow_nan=False))
            return 0
        policy_operations: dict[str, Callable[..., Any]] = {
            "compile": subject_policy.compile, "test": subject_policy.check, "explain": subject_policy.explain}
        handler = (policy_operations[args.unimem_subject_policy] if args.unimem_person == "policy"
                   else operations[args.unimem_person])
        result = handler(paths.resolve_root(args.dest), **body)
    except TypeError as exc:
        raise ValueError("UniMem personal memory request fields do not match the operation") from exc
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


COMMANDS["person"] = add_person
