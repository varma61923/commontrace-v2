"""Lazy CLI loading: only the invoked subcommand module is imported."""
from __future__ import annotations

import subprocess
import sys


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=60,
    )


def test_lazy_top_level_parser_imports_nothing():
    script = (
        "from commontrace import cli\n"
        "cli.build_parser()\n"
        "import sys\n"
        "mods = sorted(m for m in sys.modules if m.startswith('commontrace.commands.'))\n"
        "assert mods == [], mods\n"
        "print('ok')\n"
    )
    res = _run(script)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "ok"


def test_only_invoked_module_is_imported_after_parsing_query_args():
    script = (
        "from commontrace.cli import build_parser\n"
        "p = build_parser()\n"
        "ns = p.parse_args(['query', 'some task', '--lexical'])\n"
        "assert ns.command == 'query', ns\n"
        "import sys\n"
        "assert 'commontrace.commands.query_cmd' in sys.modules\n"
        "others = ('commontrace.commands.capture_cmd', 'commontrace.commands.lesson_cmd',"
        " 'commontrace.commands.doctor_cmd', 'commontrace.commands.init_cmd',"
        " 'commontrace.commands.serve_cmd')\n"
        "present = [m for m in others if m in sys.modules]\n"
        "assert not present, present\n"
        "print('ok')\n"
    )
    res = _run(script)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "ok"


def test_all_commands_resolve():
    script = (
        "import argparse\n"
        "from commontrace import cli\n"
        "p = cli.build_parser()\n"
        "top = None\n"
        "for a in p._actions:\n"
        "    import argparse as _ap\n"
        "    if isinstance(a, _ap._SubParsersAction):\n"
        "        top = a.choices\n"
        "        break\n"
        "assert top is not None\n"
        "missing = [n for n in cli._COMMANDS if n not in top]\n"
        "assert not missing, missing\n"
        "assert len(top) == len(cli._COMMANDS), (len(top), len(cli._COMMANDS))\n"
        "for name in cli._COMMANDS:\n"
        "    sub = cli.build_parser(name)\n"
        "    for a in sub._actions:\n"
        "        if isinstance(a, argparse._SubParsersAction):\n"
        "            assert list(a.choices) == [name], (name, list(a.choices))\n"
        "            break\n"
        "    else:\n"
        "        raise AssertionError(name)\n"
        "print('ok')\n"
    )
    res = _run(script)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "ok"


def test_top_level_help_works_without_importing_everything():
    script = (
        "from commontrace import cli\n"
        "p = cli.build_parser()\n"
        "text = p.format_help()\n"
        "assert 'usage:' in text, text[:500]\n"
        "for name in ('capture', 'query', 'doctor'):\n"
        "    assert name in text, (name, text[:2000])\n"
        "import sys\n"
        "mods = [m for m in sys.modules if m.startswith('commontrace.commands.')]\n"
        "assert not mods, mods\n"
        "print('ok')\n"
    )
    res = _run(script)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "ok"


def test_unknown_command_errors():
    script = (
        "import argparse\n"
        "from commontrace import cli\n"
        "assert cli.main(['xyzzy']) == 2\n"
        "assert cli.main(['captur']) == 2\n"
        "p = cli.build_parser()\n"
        "try:\n"
        "    p.parse_args(['xyzzy'])\n"
        "except SystemExit as exc:\n"
        "    assert exc.code != 0\n"
        "else:\n"
        "    raise AssertionError('unknown should exit non-zero')\n"
        "print('ok')\n"
    )
    res = _run(script)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().endswith("ok")
