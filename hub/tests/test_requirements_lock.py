from __future__ import annotations

import pathlib
import re

HUB_DIR = pathlib.Path(__file__).resolve().parent.parent
REQUIREMENTS = HUB_DIR / "requirements.txt"
LOCK = HUB_DIR / "requirements-lock.txt"

_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s#]+)")


def _parse_pins(path: pathlib.Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN_RE.match(line)
        if match:
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            pins[name] = match.group(2)
    return pins


def test_the_lock_file_is_nonempty_and_parses():
    pins = _parse_pins(LOCK)
    assert len(pins) > 30, "requirements-lock.txt looks truncated or unparseable"


def test_every_direct_pin_agrees_with_the_lock():
    direct = _parse_pins(REQUIREMENTS)
    locked = _parse_pins(LOCK)
    assert direct, "requirements.txt's own pins failed to parse -- check _PIN_RE"

    missing = sorted(set(direct) - set(locked))
    assert not missing, (
        f"direct pin(s) {missing} have no entry in requirements-lock.txt -- "
        "regenerate the lock (see its own header)"
    )

    mismatched = {
        name: (direct[name], locked[name])
        for name in direct
        if direct[name] != locked[name]
    }
    assert not mismatched, (
        f"direct pin(s) disagree with the lock (requirements.txt, lock): {mismatched} -- "
        "the lock is stale; regenerate it (see its own header)"
    )
