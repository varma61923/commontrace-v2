"""Generate SDKs with the standard Swagger-compatible OpenAPI Generator CLI.

The upstream generator and bytes are pinned. Generated files are build artifacts,
not handwritten templates. Requires Java 17+; no runtime Python dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "7.26.0"
SHA256 = "1760050094997b9cc790cc1350be095f15da8c08a996b184d3eadc4ccda5e35e"
GENERATORS = {"typescript": "typescript-fetch", "go": "go", "rust": "rust", "java": "java", "kotlin": "kotlin"}
PROPERTIES = {
    "typescript": "npmName=@commontrace/sdk,npmVersion=0.1.0,supportsES6=true,"
    "prefixParameterInterfaces=true,licenseName=AGPL-3.0-only",
    "go": "packageName=commontrace,packageVersion=0.1.0,generateInterfaces=true",
    "rust": "packageName=commontrace-sdk,packageVersion=0.1.0,reqwestDefaultFeatures=rustls",
    "java": "groupId=io.commontrace,artifactId=memory-sdk,artifactVersion=0.1.0,"
    "apiPackage=io.commontrace.api,modelPackage=io.commontrace.model,dateLibrary=java8",
    "kotlin": "packageName=io.commontrace,groupId=io.commontrace,artifactId=memory-sdk-kotlin,artifactVersion=0.1.0",
}


def checked_jar(path: Path) -> Path:
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        artifact = (
            "/maven2/org/openapitools/openapi-generator-cli/" + VERSION + "/openapi-generator-cli-" + VERSION + ".jar"
        )
        urls = [
            "https://repo.maven.apache.org" + artifact,
            "https://repo1.maven.org" + artifact,
            "https://repo.maven.apache.org" + artifact,
        ] * 2
        # Fetch into a disposable name; interrupted downloads never become the cache.
        temporary = path.with_suffix(".download")
        try:
            for attempt, url in enumerate(urls):
                try:
                    with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
                        digest, size = hashlib.sha256(), 0
                        while chunk := response.read(1024 * 1024):
                            size += len(chunk)
                            if size > 64 * 1024 * 1024:
                                raise ValueError("generator exceeds the bounded download size")
                            digest.update(chunk)
                            output.write(chunk)
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code not in (404, 408, 429, 500, 502, 503, 504) or attempt == len(urls) - 1:
                        raise
                except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
                    if attempt == len(urls) - 1:
                        raise
                # Immutable artifacts can briefly 404 on a mirror or CDN.
                # Retry with a delay; the pinned digest remains authoritative.
                time.sleep(min(16, 2 ** attempt))
            if digest.hexdigest() != SHA256:
                raise ValueError("OpenAPI Generator checksum mismatch")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    if hashlib.sha256(path.read_bytes()).hexdigest() != SHA256:
        raise ValueError("OpenAPI Generator checksum mismatch")
    return path


def kotlin_compatible_spec(source: Path, target: Path) -> Path:
    """Normalize two OpenAPI constructs that currently emit invalid Kotlin.

    OpenAPI Generator's Kotlin templates produce an unnamed enum member for the
    empty string, and treat arrays of enum values as though the list itself had
    a `.value` property. For Kotlin only, represent those fields as strings;
    the canonical contract and the other generated languages keep the exact
    enum constraints, and the server remains authoritative for validation.
    """
    document = json.loads(source.read_text(encoding="utf-8"))

    def normalize(value: object, *, array_item: bool = False) -> None:
        if isinstance(value, dict):
            enum = value.get("enum")
            if isinstance(enum, list) and ("" in enum or array_item):
                value.pop("enum", None)
            if value.get("type") == "array" and isinstance(value.get("items"), dict):
                normalize(value["items"], array_item=True)
            for key, child in value.items():
                if key != "items":
                    normalize(child)
        elif isinstance(value, list):
            for child in value:
                normalize(child)

    normalize(document)
    target.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n",
                      encoding="utf-8")
    return target


def generate(command: list[str], spec: Path, language: str, target: Path, *,
             package_version: str | None = None) -> None:
    subprocess.run(
        [
            *command,
            "generate",
            "-i",
            str(spec),
            "-g",
            GENERATORS[language],
            "-o",
            str(target),
            "--git-user-id",
            "varma61923",
            "--git-repo-id",
            "commontrace-v2",
            "--additional-properties",
            PROPERTIES[language] if package_version is None else re.sub(
                r"(npmVersion|packageVersion|artifactVersion)=[^,]+",
                lambda match: match.group(1) + "=" + package_version, PROPERTIES[language]),
            "--global-property",
            "apiDocs=false,modelDocs=false,apiTests=false,modelTests=false",
        ],
        check=True,
    )
    if language == "kotlin":
        normalize_kotlin_transport(target)


def normalize_kotlin_transport(target: Path) -> None:
    """Repair the pinned template's unreachable nullable OkHttp body branch.

    OkHttp 5 guarantees a non-null Response.body. The upstream template retains
    an Elvis fallback from OkHttp 4, which Kotlin diagnoses as always unreachable.
    This bounded generation repair adds no SDK operations or handwritten client.
    Fail on template drift rather than changing unknown generated transport code.
    """
    source = target / "src/main/kotlin/io/commontrace/infrastructure/ApiClient.kt"
    text = source.read_text(encoding="utf-8")
    expression = "val body = response.body ?: return null"
    if text.count(expression) != 1:
        raise ValueError("pinned Kotlin transport template changed; review generator compatibility")
    source.write_text(text.replace(expression, "val body = response.body"), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "sdk" / "generated")
    parser.add_argument("--jar", type=Path)
    parser.add_argument("--languages", nargs="+", choices=GENERATORS, default=list(GENERATORS))
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--api", choices=("memory", "gateway", "hub"), default="memory",
                        help="Generate the memory subset (default), all gateway routes, or the Hub REST API")
    parser.add_argument("--package-version", help="release version for generated package manifests (SemVer)")
    args = parser.parse_args()
    if args.package_version is not None and not re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?",
            args.package_version):
        parser.error("--package-version must be a SemVer release or prerelease")
    jar = checked_jar(args.jar or ROOT / "build" / ("openapi-generator-cli-" + VERSION + ".jar"))
    spec = {"memory": ROOT / "sdk" / "openapi.json", "gateway": ROOT / "openapi" / "gateway.json",
            "hub": ROOT / "hub" / "openapi.json"}[args.api]
    command = ["java", "-jar", str(jar)]
    # The full gateway contract is validated with the same pinned tool; clients are
    # generated from the memory subset only.
    for document in (ROOT / "sdk" / "openapi.json", ROOT / "openapi" / "gateway.json",
                     ROOT / "hub" / "openapi.json"):
        subprocess.run([*command, "validate", "-i", str(document)], check=True)
    if args.validate_only:
        return 0
    for language in args.languages:
        target = args.output / language
        if target.is_symlink() or (target.exists() and any(target.iterdir())):
            parser.error("generation requires a fresh output directory: " + str(target))
        if language == "kotlin":
            with tempfile.TemporaryDirectory(prefix="commontrace-kotlin-openapi-") as directory:
                normalized = kotlin_compatible_spec(spec, Path(directory) / "openapi.json")
                generate(command, normalized, language, target, package_version=args.package_version)
        else:
            generate(command, spec, language, target, package_version=args.package_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
