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
            relative = pathlib.PurePosixPath(line.removeprefix("!commontrace/")).with_suffix("")
            parts = relative.parts
            if parts[-1] == "__init__" and len(parts) > 1:
                parts = parts[:-1]
            allowed.add(".".join(parts))
    return allowed


def _module_path(name: str) -> pathlib.Path | None:
    path = REPO_ROOT / "commontrace" / pathlib.Path(*name.split("."))
    if path.with_suffix(".py").is_file():
        return path.with_suffix(".py")
    if (path / "__init__.py").is_file():
        return path / "__init__.py"
    return None


def _is_submodule(name: str) -> bool:
    return _module_path(name) is not None


def _client_imports_in(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = path.parent.relative_to(REPO_ROOT).parts

    def add(name):
        if not name.startswith("commontrace."):
            return
        name = name.removeprefix("commontrace.")
        if not _is_submodule(name):
            return
        found.add(name)
        # Importing a child also executes each parent package's __init__.
        for count in range(1, len(name.split("."))):
            parent = ".".join(name.split(".")[:count])
            if _is_submodule(parent):
                found.add(parent)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join((*package[:len(package) - node.level + 1], *base.split("."))).rstrip(".")
            add(base)
            for alias in node.names:
                add(base + "." + alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "import_module" and node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    add(node.args[0].value)
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
        path = _module_path(module)
        if path is None:
            continue
        for dep in sorted(_client_imports_in(path)):
            if dep not in found and _is_submodule(dep):
                found[dep] = f"needed by {path.relative_to(REPO_ROOT)}"
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
            "module is import-safe with hub/requirements.txt. Package children "
            "also require their parent __init__.py."
        )

    def test_every_allowlisted_file_exists(self):
        for module in _allowlisted_modules():
            path = _module_path(module)
            assert path is not None, f".dockerignore re-includes {module}, which does not exist"

    def test_the_allowlist_is_not_wider_than_the_imports(self):
        imported = set(_imported_modules())
        extra = _allowlisted_modules() - imported - {"__init__"}
        assert not extra, (
            "`.dockerignore` ships commontrace modules that no hub/ file imports "
            f"any more: {sorted(extra)}. Drop the negation, or note why the image "
            "needs it."
        )


def _required_import_nodes(tree, *, eager_only=False):
    optional = set()
    for block in ast.walk(tree):
        if isinstance(block, ast.Try) and any(isinstance(h.type, ast.Name) and h.type.id == "ImportError"
                                             for h in block.handlers):
            for statement in block.body:
                optional.update(ast.walk(statement))
        if eager_only and isinstance(block, (ast.FunctionDef, ast.AsyncFunctionDef)):
            optional.update(ast.walk(block))
    return [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in optional]


class TestImportedModulesAreStdlibOnly:
    def test_no_shipped_client_module_import_needs_a_dependency_the_image_lacks(self):
        import sys

        hub_requirements = {
            line.split("[")[0].split("==")[0].split(">=")[0].strip().lower().replace("-", "_")
            for line in (REPO_ROOT / "hub" / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        # Distribution names need not match their import names. These wheels
        # are explicitly pinned by the server, rather than optional exemptions.
        distribution_imports = {"pyyaml": "yaml", "pyjwt": "jwt", "argon2_cffi": "argon2"}
        hub_requirements |= {name for distribution, name in distribution_imports.items()
                             if distribution in hub_requirements}
        available = set(sys.stdlib_module_names) | hub_requirements | {"commontrace"}

        for module in sorted(_allowlisted_modules()):
            path = _module_path(module)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in _required_import_nodes(tree, eager_only=True):
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


def test_only_explicit_optional_import_guards_exempt_dependencies():
    tree = ast.parse("import required\ntry:\n import optional\nexcept ImportError:\n import fallback\n"
                     "try:\n import still_required\nexcept ValueError:\n pass\n")
    assert {node.names[0].name for node in _required_import_nodes(tree)} == {
        "required", "fallback", "still_required"}


def test_dependency_check_distinguishes_import_time_from_lazy_feature_dependencies():
    tree = ast.parse("import required\ndef optional_feature():\n import optional_provider\n"
                     "class Eager:\n import class_dependency\n def method(self):\n  import lazy_method\n")
    assert {node.names[0].name for node in _required_import_nodes(tree, eager_only=True)} == {
        "required", "class_dependency"}


def test_import_closure_includes_package_initializers_children_and_relative_imports(tmp_path, monkeypatch):
    import sys

    module = sys.modules[__name__]
    package = tmp_path / "commontrace" / "nested"
    package.mkdir(parents=True)
    (tmp_path / "commontrace" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("from .impl import Value\n")
    (package / "impl.py").write_text("from . import sibling\nValue = 1\n")
    (package / "sibling.py").write_text("")
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "app.py").write_text("from commontrace.nested import impl\n")
    ignore = tmp_path / ".dockerignore"
    ignore.write_text("commontrace/**\n!commontrace/nested/__init__.py\n!commontrace/nested/impl.py\n")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "HUB_DIR", hub)
    monkeypatch.setattr(module, "DOCKERIGNORE", ignore)
    assert _allowlisted_modules() == {"nested", "nested.impl"}
    assert set(_imported_modules()) == {"nested", "nested.impl", "nested.sibling"}


def test_shipped_core_imports_and_canonical_runtime_work_without_checkout_fallback(tmp_path):
    """Copy only selected image sources, then import from that isolated package."""
    import json
    import os
    import shutil
    import subprocess
    import sys

    modules = sorted(_allowlisted_modules())
    for module in modules:
        source = _module_path(module)
        destination = tmp_path / source.relative_to(REPO_ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    for relative in ("commontrace/configuration.schema.json", "commontrace/ui/tokens.css"):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, destination)
        assert "!" + relative in DOCKERIGNORE.read_text()
    # -S removes editable-install .pth hooks and user-site packages. Add only
    # third-party installation directories for the Hub's pinned dependencies.
    sites = [path for path in sys.path if path.endswith(("site-packages", "dist-packages"))]
    code = """
import importlib, json, pathlib, sys
root = pathlib.Path.cwd()
sys.path.extend(json.loads(sys.argv[1]))
for module in json.loads(sys.argv[2]):
    importlib.import_module('commontrace.' + module if module != '__init__' else 'commontrace')
from commontrace.memory_record import atomic_fact_from_record, atomic_fact_record
from commontrace.hierarchical import AtomicFact
from commontrace.conversation import embed
from commontrace.vector_store import SQLiteVectorIndex
from commontrace.vector_snapshots import SnapshotBackend
record = atomic_fact_record(AtomicFact(
    id='fact', statement='A bounded package smoke fact', category='general', scopes=[],
    confidence=0.8, confirmations=1, valid_from='2026-01-01T00:00:00Z', valid_until=None,
), 1)
assert atomic_fact_from_record(record).statement == 'A bounded package smoke fact'
assert embed.model_identity('bge-small')
for name, module in list(sys.modules.items()):
    if name == 'commontrace' or name.startswith('commontrace.'):
        path = getattr(module, '__file__', None)
        assert path and pathlib.Path(path).resolve().is_relative_to(root), (name, path)
print('isolated imports and canonical fact round-trip passed')
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", code, json.dumps(sites), json.dumps(modules)], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)}, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_image_telemetry_runs_with_no_optional_packages(tmp_path):
    import json
    import os
    import shutil
    import subprocess
    import sys

    package = tmp_path / "commontrace"
    package.mkdir()
    for name in ("__init__.py", "telemetry.py", "memory_guard.py", "paths.py",
                 "configuration.py", "configuration.schema.json", "offline.py", "exceptions.py"):
        shutil.copyfile(REPO_ROOT / "commontrace" / name, package / name)
    code = ("from commontrace import telemetry; import logging; "
            "telemetry.configure_logging('json', 'INFO', log_file='private.jsonl'); "
            "logging.getLogger('commontrace').info('Bearer fake-credential'); "
            "assert not telemetry.status()['otel_installed']")
    result = subprocess.run([sys.executable, "-S", "-c", code], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(tmp_path), "COMMONTRACE_OTEL": "1"},
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    row = json.loads((tmp_path / "private.jsonl").read_text())
    assert "fake-credential" not in json.dumps(row)
    assert row["level"] == "info"


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
