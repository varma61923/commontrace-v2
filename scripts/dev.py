"""Dependency-free development tasks and source-derived reference inventories."""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def references() -> dict[str, str]:
    environment: dict[str, tuple[str, int]] = {}
    for package in ('commontrace', 'hub'):
        for path in sorted((ROOT / package).rglob('*.py')):
            if 'tests' in path.parts:
                continue
            source = path.read_text(encoding='utf-8')
            for match in re.finditer(r'\b(?:COMMONTRACE|HUB)_[A-Z0-9_]+\b', source):
                name = match.group()
                if not name.endswith('_'):
                    environment.setdefault(
                        name, (path.relative_to(ROOT).as_posix(), source.count('\n', 0, match.start()) + 1))
    env = ['# Environment reference', '',
           'Source-referenced configuration names; reads source, never credentials or the runtime environment.',
           'Defaults and validation are in the linked implementations. Secret keys/tokens belong in',
           'deployment secret settings. Optional inference providers can remain unset for local-only operation.', '',
           'Dynamic provider prefixes and test-only variables are described in the relevant provider/test setup.', '',
           '| Name | First source reference |', '| --- | --- |']
    env.extend(f'| `{name}` | [{path}:{line}](../{path}#L{line}) |'
               for name, (path, line) in sorted(environment.items()))
    api = ['# MCP and gateway API reference', '',
           'Generated from statically decorated tool definitions. Each source link includes full parameters, defaults,',
           'validation and return documentation. Optional tools depend on deployment capabilities/configuration.',
           'CLI commands expose `--help`; Python APIs can be browsed with `python -m pydoc commontrace`.', '',
           'Local MCP configuration and Hub bearer authentication examples are in [README](../README.md)',
           'and [Hub README](../hub/README.md). HTTP/JSON-line gateway contracts remain versioned `/v1`.', '',
           'Batch tools accept 1–25 items and return ordered `results`; commits are independent.',
           'Use per-contribution idempotency keys. Batch delete is permanent; do not replay it automatically.', '']
    for relative in ('commontrace/mcp_server.py', 'hub/server.py'):
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding='utf-8'))
        api.extend([f'## {relative}', '', '| Tool | Parameters | Description / source |', '| --- | --- | --- |'])
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [ast.unparse(d.func if isinstance(d, ast.Call) else d) for d in node.decorator_list]
            if not any(d.endswith('.tool') or d == 'scoped_tool' for d in decorators):
                continue
            description = (ast.get_docstring(node) or 'See source contract.').split('\n')[0].replace('|', '\\|')
            arguments = ', '.join(a.arg for a in node.args.args + node.args.kwonlyargs) or 'none'
            api.append(f'| `{node.name}` | `{arguments}` | {description} [source](../{relative}#L{node.lineno}) |')
        api.append('')
    api.extend(['## Gateway HTTP routes', '', '| Method | Route |', '| --- | --- |'])
    tree = ast.parse((ROOT / 'commontrace/gateway.py').read_text(encoding='utf-8'))
    routes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple) and len(node.elts) == 2 and all(isinstance(x, ast.Constant) for x in node.elts):
            method, route = (x.value for x in node.elts)
            if method in ('GET', 'POST', 'DELETE', 'PUT', 'PATCH') and isinstance(route, str) and route.startswith('/'):
                routes.add((method, route))
    api.extend(f'| {method} | `{route}` |' for method, route in sorted(routes))
    return {'docs/environment.md': '\n'.join(env) + '\n', 'docs/api.md': '\n'.join(api) + '\n'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task', choices=('test', 'lint', 'check', 'serve', 'docs'))
    parser.add_argument('--hub', action='store_true', help='run PostgreSQL Hub tests (disposable database required)')
    parser.add_argument('--check', action='store_true', help='check generated documentation without writing')
    parser.add_argument('--dest', help='gateway store root for serve')
    args, extra = parser.parse_known_args()
    if args.task == 'docs':
        if extra:
            parser.error('docs accepts no additional arguments')
        stale = []
        for relative, content in references().items():
            path = ROOT / relative
            if args.check:
                if not path.exists() or path.read_text(encoding='utf-8') != content:
                    stale.append(relative)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding='utf-8')
        if stale:
            print('Stale generated references: ' + ', '.join(stale), file=sys.stderr)
        return bool(stale)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    if args.task == 'test':
        if args.hub and not os.environ.get('HUB_TEST_DATABASE_URL'):
            parser.error('--hub requires HUB_TEST_DATABASE_URL pointing at a disposable database')
        command = [sys.executable, '-m', 'pytest', *(['hub/tests'] if args.hub else ['tests', 'e2e_tests']),
                   '-p', 'no:cacheprovider', *extra]
    elif args.task == 'lint':
        command = [sys.executable, '-m', 'ruff', 'check', '.', '--no-cache', *extra]
    elif args.task == 'check':
        command = ['git', 'diff', '--check', *extra]
    else:
        command = [sys.executable, '-m', 'commontrace', 'gateway', 'serve',
                   *(['--dest', args.dest] if args.dest else []), *extra]
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
