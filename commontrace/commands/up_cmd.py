"""One-command local server with the same scoped HTTP memory API."""

from __future__ import annotations

from commontrace import cli, paths


def add_parser(subparsers):
    parser = subparsers.add_parser("up", help="Initialize a store and run its local memory API and console.")
    parser.add_argument("--dest", default=".")
    parser.add_argument("--host", default=None)
    parser.add_argument("--profile", choices=("local", "team", "server"))
    parser.add_argument("--setup-only", action="store_true", help="Configure the runtime and exit without serving.")
    parser.add_argument("--agent", action="append", default=[], help="Generate this coding agent's hook kit.")
    parser.add_argument(
        "--allow-global-agent-config",
        action="store_true",
        help="Allow detected agents such as Kimi to update their host-global hook config.",
    )
    parser.add_argument(
        "--project-rule", metavar="FILE", help="Stage an explicit project rule for review before activation.",
    )
    parser.add_argument(
        "--import-agent-history",
        action="store_true",
        help="Import bounded Claude Code and Codex history attributable to this project from your home directory.",
    )
    parser.add_argument("--port", type=int, default=None)
    parser.set_defaults(func=run)


def run(args):
    root = paths.resolve_root(args.dest)
    result = cli.main(["init", "--dest", root])
    if result:
        return result
    import json

    from commontrace import configuration
    from commontrace.bootstrap_runtime import setup, stage_project_rule

    config = configuration.load(root, profile=args.profile)
    staged = None
    if getattr(args, "project_rule", None):
        try:
            staged = stage_project_rule(root, args.project_rule)
        except (OSError, ValueError) as exc:
            import sys

            print(f"[commontrace] project rule could not be staged: {exc}", file=sys.stderr)
            return 2
    result = setup(
        root,
        config=config,
        agents=args.agent or None,
        allow_global_agent_config=getattr(args, "allow_global_agent_config", False),
        import_agent_history=getattr(args, "import_agent_history", False),
    )
    if staged is not None:
        result["project_rule"] = staged
    print(json.dumps(result, indent=2))
    if args.setup_only:
        return 0
    import subprocess
    import sys

    with configuration.apply(config):
        broker = subprocess.Popen(
            [sys.executable, "-m", "commontrace.hook_service", "--dest", root, "--profile", config["profile"]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            return cli.main(
                [
                    "gateway",
                    "--dest",
                    root,
                    "--host",
                    args.host or config["host"],
                    "--port",
                    str(args.port or config["port"]),
                ]
            )

        finally:
            broker.terminate()
            try:
                broker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                broker.kill()
                broker.wait(timeout=5)
