"""The server image must contain every `commontrace` module the Hub imports.

WHY THIS FILE EXISTS
--------------------
`Dockerfile` copies `commontrace/` into the image, and `.dockerignore`
narrows that to an explicit allowlist -- the client package is a separate
pip-installable thing and deliberately does not ship in the server image,
except for the few modules `hub/` genuinely imports (see `hub/commons.py`
on MinHash and `hub/outcomes.py` on the outcome statistics).

An allowlist that falls behind the imports does not fail at build time. It
fails when the container STARTS, with an ImportError on a module that
imports perfectly well in every test run, in every editable install, and
on every developer's machine -- because all of those have the whole client
package on the path. It is invisible right up to the moment a deployment
tries to boot.

This is not hypothetical. Adding `from commontrace import experiment` to
`hub/outcomes.py` turned CI's two Docker jobs red exactly this way, while
all 1256 tests, ruff, bandit and `alembic check` stayed green:

    File "/app/hub/outcomes.py", line 66, in <module>
        from commontrace import experiment
    ImportError: cannot import name 'experiment' from 'commontrace'

The Dockerfile's own comment claimed `hub/tests/test_commons.py` pinned
the allowlist. It did not -- no test anywhere did. This one does, and it
runs in every job rather than only in the two Docker ones, so the gap is
caught before a push instead of after it.
"""
from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
HUB_DIR = REPO_ROOT / "hub"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _allowlisted_modules() -> set[str]:
    """The `commontrace` modules `.dockerignore` re-includes, by module name.

    Parsed from the file rather than restated here on purpose: a second
    hand-maintained copy of this list is the same failure mode one level up.
    """
    allowed = set()
    for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("!commontrace/") and line.endswith(".py"):
            allowed.add(pathlib.PurePosixPath(line[1:]).stem)
    return allowed


def _is_submodule(name: str) -> bool:
    """Whether `from commontrace import <name>` pulls in a FILE that has to
    ship, or just a name re-exported from `__init__.py`.

    `hub/__init__.py` does `from commontrace import __version__`, which
    needs no separate file in the image -- `__init__.py` already ships.
    Treating every imported name as a module would demand a
    `!commontrace/__version__.py` negation for a file that does not and
    should not exist.
    """
    return (REPO_ROOT / "commontrace" / f"{name}.py").is_file()


def _client_imports_in(path: pathlib.Path) -> set[str]:
    """Every `commontrace` SUBMODULE one Python file imports.

    AST, not a regex: `from commontrace import overlap` and
    `import commontrace.overlap` are different nodes, and a regex over
    source would also match the many prose mentions of these module names
    in this codebase's comments.
    """
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
    """{module name: why it has to ship} for every `commontrace` submodule
    the image needs, TRANSITIVELY.

    The transitive part is not a refinement, it is the difference between
    this file working and not working. A shipped client module that imports
    a second client module (`retrieval.py` imports `_lexical.py`) breaks
    container start exactly the way the docstring above describes -- and
    under a direct-imports-only reading, the module that fixes it looks like
    an unused negation, so `test_the_allowlist_is_not_wider_than_the_imports`
    would demand its DELETION. A guard that instructs you to reintroduce the
    bug it exists to prevent is worse than no guard.

    Closed as a fixpoint over the client package rather than one level deep,
    because two levels is not a principled stopping point either.
    """
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
        """The check that would have caught the ImportError above before it
        reached CI."""
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
        """A stale negation for a renamed or deleted module is silent in
        Docker -- COPY simply matches nothing -- so it would rot unnoticed."""
        for module in _allowlisted_modules():
            path = REPO_ROOT / "commontrace" / f"{module}.py"
            assert path.is_file(), f".dockerignore re-includes {path}, which does not exist"

    def test_the_allowlist_is_not_wider_than_the_imports(self):
        """The image ships the smallest client surface that works. A module
        left on the allowlist after the import that needed it is gone is not
        a failure, but it is dead weight in a server image and a reader
        would reasonably assume the Hub still depends on it.

        `__init__.py` is exempt: it is what makes the directory a package,
        so it ships regardless of which submodules do.
        """
        imported = set(_imported_modules())
        extra = _allowlisted_modules() - imported - {"__init__"}
        assert not extra, (
            "`.dockerignore` ships commontrace modules that no hub/ file imports "
            f"any more: {sorted(extra)}. Drop the negation, or note why the image "
            "needs it."
        )


class TestImportedModulesAreStdlibOnly:
    def test_no_shipped_client_module_needs_a_dependency_the_image_lacks(self):
        """The image installs `hub/requirements.txt` only. A shipped client
        module that imports numpy, yaml or anything else outside the stdlib
        would pass this file's other tests and still fail at container
        start -- the same invisible-until-boot failure, one layer down.

        Checked against the Hub's own declared requirements rather than a
        hardcoded stdlib list, so a module that imports something the Hub
        genuinely does install (sqlalchemy, say) is correctly allowed.
        """
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


def test_every_base_image_is_pinned_by_digest():
    """A tag can be re-pushed; a digest cannot. Pinned, what gets built and
    run is exactly what was tested, and Dependabot (docker and
    docker-compose ecosystems) proposes each new digest as a reviewed PR."""
    import re

    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    images = re.findall(r"^FROM\s+(\S+)", dockerfile, flags=re.M)
    images += re.findall(r"^\s+image:\s*(\S+)", compose, flags=re.M)
    assert images
    unpinned = [i for i in images if not re.search(r"@sha256:[0-9a-f]{64}$", i)]
    assert not unpinned, f"pin by digest (image:tag@sha256:...): {unpinned}"
