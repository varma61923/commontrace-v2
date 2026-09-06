"""`hub/requirements-lock.txt` is a pip constraints file pinning every
transitive dependency underneath `hub/requirements.txt`'s direct pins (see
that file's own header for why it exists and why it's scoped to one Python
version). A lock file nobody checks for drift is worse than no lock file at
all: if a direct pin in requirements.txt changes and the lock isn't
regenerated, `pip install -r ... -c ...` either fails outright (a version
conflict between -r and -c) or -- if the two files never disagree on the
SAME package, just have a stale transitive tree -- silently keeps installing
what the old lock says instead of what changed. This file catches the first
half (direct pins must agree with the lock) directly; the second half (a
transitive dependency the direct install pattern would have picked up) is
what hub/tests/test_image_contents.py + a real image build already exercise
against the constrained install.
"""
from __future__ import annotations

import pathlib
import re

HUB_DIR = pathlib.Path(__file__).resolve().parent.parent
REQUIREMENTS = HUB_DIR / "requirements.txt"
LOCK = HUB_DIR / "requirements-lock.txt"

_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s#]+)")


def _parse_pins(path: pathlib.Path) -> dict[str, str]:
    """{normalized package name: version} for every `name==version` line.
    Comments (a bare `#...` line, or trailing `  # ...` on a pin line) are
    ignored; normalization matches pip's own (case-insensitive, `-`/`_`
    treated the same) so `PyJWT` and `pyjwt` are recognized as the same
    package."""
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
    """A version bump in requirements.txt that never made it into the lock
    is exactly the drift this file exists to catch -- `pip install -r ... -c
    ...` would refuse to resolve it (a real conflict), but failing a build
    is a worse way to discover that than failing this test."""
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
