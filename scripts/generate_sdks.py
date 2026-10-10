"""Generate SDKs with the standard Swagger-compatible OpenAPI Generator CLI.

The upstream generator and bytes are pinned. Generated files are build artifacts,
not handwritten templates. Requires Java 17+; no runtime Python dependencies.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "7.26.0"
SHA256 = "1760050094997b9cc790cc1350be095f15da8c08a996b184d3eadc4ccda5e35e"
GENERATORS = {"typescript": "typescript-fetch", "go": "go", "rust": "rust", "java": "java", "kotlin": "kotlin"}
PROPERTIES = {
    "typescript": "npmName=@commontrace/sdk,npmVersion=0.1.0,supportsES6=true",
    "go": "packageName=commontrace,packageVersion=0.1.0,generateInterfaces=true",
    "rust": "packageName=commontrace-sdk,packageVersion=0.1.0,reqwestDefaultFeatures=rustls",
    "java": "groupId=io.commontrace,artifactId=memory-sdk,artifactVersion=0.1.0,"
            "apiPackage=io.commontrace.api,modelPackage=io.commontrace.model,dateLibrary=java8",
    "kotlin": "packageName=io.commontrace,groupId=io.commontrace,artifactId=memory-sdk-kotlin,artifactVersion=0.1.0",
}


def checked_jar(path: Path) -> Path:
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = ("https://repo.maven.apache.org/maven2/org/openapitools/openapi-generator-cli/"+VERSION+
               "/openapi-generator-cli-"+VERSION+".jar")
        # Fetch into a disposable name; interrupted downloads never become the cache.
        temporary = path.with_suffix(".download")
        try:
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
                digest, size = hashlib.sha256(), 0
                while chunk := response.read(1024*1024):
                    size += len(chunk)
                    if size > 64*1024*1024:
                        raise ValueError("generator exceeds the bounded download size")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != SHA256:
                raise ValueError("OpenAPI Generator checksum mismatch")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    if hashlib.sha256(path.read_bytes()).hexdigest() != SHA256:
        raise ValueError("OpenAPI Generator checksum mismatch")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT/"sdk"/"generated")
    parser.add_argument("--jar", type=Path)
    parser.add_argument("--languages", nargs="+", choices=GENERATORS, default=list(GENERATORS))
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    jar = checked_jar(args.jar or ROOT/"build"/("openapi-generator-cli-"+VERSION+".jar"))
    spec = ROOT/"sdk"/"openapi.json"
    command = ["java", "-jar", str(jar)]
    # The full gateway contract is validated with the same pinned tool; clients are
    # generated from the memory subset only.
    for document in (spec, ROOT/"openapi"/"gateway.json"):
        subprocess.run([*command, "validate", "-i", str(document)], check=True)
    if args.validate_only:
        return 0
    for language in args.languages:
        target = args.output/language
        if target.is_symlink() or (target.exists() and any(target.iterdir())):
            parser.error("generation requires a fresh output directory: "+str(target))
        subprocess.run([*command, "generate", "-i", str(spec), "-g", GENERATORS[language], "-o", str(target),
            "--additional-properties", PROPERTIES[language], "--global-property",
            "apiDocs=false,modelDocs=false,apiTests=false,modelTests=false"], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
