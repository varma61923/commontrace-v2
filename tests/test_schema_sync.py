from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_SCHEMAS = REPO_ROOT / "protocol" / "schemas"
PACKAGE_SCHEMAS = REPO_ROOT / "commontrace" / "schemas"


def _schema_names() -> list[str]:
    return sorted(p.name for p in PROTOCOL_SCHEMAS.glob("*.json"))


@pytest.mark.parametrize("name", _schema_names())
def test_packaged_schema_matches_protocol_schema(name):
    protocol_text = (PROTOCOL_SCHEMAS / name).read_text(encoding="utf-8")
    package_path = PACKAGE_SCHEMAS / name
    assert package_path.exists(), f"commontrace/schemas/{name} is missing (protocol/schemas/ has it)"
    package_text = package_path.read_text(encoding="utf-8")
    assert protocol_text == package_text, (
        f"protocol/schemas/{name} and commontrace/schemas/{name} have drifted. "
        "commontrace/validate.py loads the commontrace/schemas/ copy at runtime, "
        "so a fix applied only to protocol/ is not actually enforced."
    )


def test_no_extra_schemas_in_package_copy():
    package_names = sorted(p.name for p in PACKAGE_SCHEMAS.glob("*.json"))
    assert package_names == _schema_names()
