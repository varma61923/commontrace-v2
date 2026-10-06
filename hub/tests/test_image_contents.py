from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
HUB_DIR = REPO_ROOT / "hub"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _allowlisted_modules() -> set[str]:
    allowed = set()
    for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("!commontrace/") and line.endswith(".py"):
            allowed.add(pathlib.PurePosixPath(line[1:]).stem)
    return allowed


def _is_submodule(name: str) -> bool:
    return (REPO_ROOT / "commontrace" / f"{name}.py").is_file()


def _client_imports_in(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "commontrace" and node.level == 0:
                found |= {a.name for a in node.names if _is_submodule(a.name)}
            elif node.module and node.module.startswith("commontrace.") and node.level == 0:
                found.add(node.module.split(".", 1)[1].split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("commontrace."):
                    found.add(alias.name.split(".", 1)[1].split(".")[0])
    return found


def _imported_modules() -> dict[str, str]:
    found: dict[str, str] = {}
    frontier: list[str] = []
    for path in sorted(HUB_DIR.rglob("*.py")):
        if "tests" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        for module in sorted(_client_imports_in(path)):
            if module not in found:
                found[module] = f"imported by {rel}"
                frontier.append(module)

    while frontier:
        module = frontier.pop()
        path = REPO_ROOT / "commontrace" / f"{module}.py"
        if not path.is_file():
            continue
        for dep in sorted(_client_imports_in(path)):
            if dep not in found and _is_submodule(dep):
                found[dep] = f"needed by commontrace/{module}.py"
                frontier.append(dep)
    return found


class TestImageCarriesEveryImportedClientModule:
    def test_every_imported_commontrace_module_ships_in_the_image(self):
        allowed = _allowlisted_modules()
        missing = {
            module: source
            for module, source in _imported_modules().items()
            if module not in allowed
        }
        assert not missing, (
            "hub/ imports commontrace modules that .dockerignore keeps out of the "
            "server image, so the container will start and immediately die with an "
            "ImportError:\n"
            + "\n".join(f"  commontrace.{m}  ({why})" for m, why in sorted(missing.items()))
            + "\n\nAdd `!commontrace/<module>.py` to .dockerignore, and check the "
            "module is import-safe in the image: it must be pure stdlib, since the "
            "image installs only hub/requirements.txt."
        )

    def test_every_allowlisted_file_exists(self):
        for module in _allowlisted_modules():
            path = REPO_ROOT / "commontrace" / f"{module}.py"
            assert path.is_file(), f".dockerignore re-includes {path}, which does not exist"

    def test_the_allowlist_is_not_wider_than_the_imports(self):
        imported = set(_imported_modules())
        extra = _allowlisted_modules() - imported - {"__init__"}
        assert not extra, (
            "`.dockerignore` ships commontrace modules that no hub/ file imports "
            f"any more: {sorted(extra)}. Drop the negation, or note why the image "
            "needs it."
        )


class TestImportedModulesAreStdlibOnly:
    def test_no_shipped_client_module_needs_a_dependency_the_image_lacks(self):
        import sys

        hub_requirements = {
            line.split("[")[0].split("==")[0].split(">=")[0].strip().lower().replace("-", "_")
            for line in (REPO_ROOT / "hub" / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        available = set(sys.stdlib_module_names) | hub_requirements | {"commontrace"}

        for module in sorted(_allowlisted_modules()):
            path = REPO_ROOT / "commontrace" / f"{module}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module.split(".")[0]]
                for name in names:
                    assert name in available, (
                        f"commontrace/{module}.py ships in the server image and imports "
                        f"{name!r}, which is neither stdlib nor in hub/requirements.txt. "
                        "The container would start and then fail on this import."
                    )


def test_packaged_theme_loads_without_installed_client_or_dependencies(tmp_path):
    """Server-only installs still need the resource used at import time."""
    import os
    import shutil
    import subprocess
    import sys

    package = tmp_path / "commontrace"
    package.mkdir()
    for name in ("__init__.py", "ui_style.py"):
        shutil.copyfile(REPO_ROOT / "commontrace" / name, package / name)
    (package / "ui").mkdir()
    shutil.copyfile(REPO_ROOT / "commontrace" / "ui" / "tokens.css", package / "ui" / "tokens.css")
    result = subprocess.run(
        [sys.executable, "-S", "-c", "from commontrace.ui_style import THEME_CSS; assert '--accent:' in THEME_CSS"],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(tmp_path)}, capture_output=True, text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "!commontrace/ui/tokens.css" in DOCKERIGNORE.read_text()


def test_every_base_image_is_pinned_by_digest():
    import re

    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    images = re.findall(r"^FROM\s+(\S+)", dockerfile, flags=re.M)
    images += re.findall(r"^\s+image:\s*(\S+)", compose, flags=re.M)
    assert images
    unpinned = [i for i in images if not re.search(r"@sha256:[0-9a-f]{64}$", i)]
    assert not unpinned, f"pin by digest (image:tag@sha256:...): {unpinned}"
