from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CalledProcessError

from scripts.build_local_image import build_local_image, copy_allowlisted_context


def make_checkout(root: Path) -> None:
    (root / "Dockerfile.local").write_text("FROM scratch\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / "README.md").write_text("fixture readme\n", encoding="utf-8")
    (root / "LICENSE").write_text("fixture license\n", encoding="utf-8")
    (root / "unimem" / "nested").mkdir(parents=True)
    (root / "unimem" / "__init__.py").write_text("", encoding="utf-8")
    (root / "unimem" / "nested" / "module.py").write_text("value = 1\n", encoding="utf-8")
    (root / "unimem" / "nested" / "module.pyc").write_bytes(b"bytecode")
    (root / "unimem" / "__pycache__").mkdir()
    (root / "unimem" / "__pycache__" / "module.pyc").write_bytes(b"bytecode")
    (root / "protocol").mkdir()
    (root / "protocol" / "schema.json").write_text("{}\n", encoding="utf-8")
    (root / "deploy" / "docker").mkdir(parents=True)
    (root / "deploy" / "docker" / "local-entrypoint.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / ".dockerignore").write_text("must not be copied\n", encoding="utf-8")
    (root / "Dockerfile.local.dockerignore").write_text("must not be copied\n", encoding="utf-8")
    (root / "hub" / "secrets").mkdir(parents=True)
    (root / "hub" / "secrets" / "private.txt").write_text("outside allowlist\n", encoding="utf-8")


class BuildLocalImageTests(unittest.TestCase):
    def test_build_uses_only_allowlisted_files_and_cleans_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            checkout = root / "checkout"
            checkout.mkdir()
            make_checkout(checkout)
            seen: dict[str, object] = {}

            def runner(command: list[str], check: bool) -> None:
                self.assertTrue(check)
                self.assertEqual(command[:4], ["docker", "build", "--tag", "fixture:local"])
                dockerfile = command[5]
                context = command[6]
                seen["context"] = context
                self.assertEqual(os.path.basename(dockerfile), "Dockerfile.local")
                self.assertEqual(Path(dockerfile).parent, Path(context))
                files = {
                    os.path.relpath(os.path.join(directory, filename), context)
                    for directory, _, filenames in os.walk(context)
                    for filename in filenames
                }
                self.assertEqual(
                    files,
                    {
                        "Dockerfile.local",
                        "pyproject.toml",
                        "README.md",
                        "LICENSE",
                        "unimem/__init__.py",
                        "unimem/nested/module.py",
                        "protocol/schema.json",
                        "deploy/docker/local-entrypoint.sh",
                    },
                )
                self.assertNotIn(".dockerignore", files)
                self.assertNotIn("Dockerfile.local.dockerignore", files)

            build_local_image(str(checkout), "fixture:local", run=runner)
            self.assertFalse(os.path.exists(seen["context"]))

    def test_build_cleans_context_when_docker_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            checkout = Path(temporary_root) / "checkout"
            checkout.mkdir()
            make_checkout(checkout)
            seen_context: list[str] = []

            def failing_runner(command: list[str], check: bool) -> None:
                self.assertTrue(check)
                seen_context.append(command[-1])
                raise CalledProcessError(1, command)

            with self.assertRaises(CalledProcessError):
                build_local_image(str(checkout), "fixture:failed", run=failing_runner)
            self.assertEqual(len(seen_context), 1)
            self.assertFalse(os.path.exists(seen_context[0]))

    def test_copy_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            checkout = root / "checkout"
            checkout.mkdir()
            make_checkout(checkout)
            external = root / "outside.py"
            external.write_text("secret = True\n", encoding="utf-8")
            (checkout / "unimem" / "escape.py").symlink_to(external)
            context = root / "context"

            with self.assertRaisesRegex(ValueError, "symlinks are not allowed"):
                copy_allowlisted_context(str(checkout), str(context))


if __name__ == "__main__":
    unittest.main()
