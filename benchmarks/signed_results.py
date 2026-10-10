"""Ed25519 signatures for explicitly public aggregate release measurements.

Signatures identify the publisher and prevent edits; they do not certify a
benchmark's scientific validity. Keep private keys outside repositories. Readers
must pin a trusted public key, rather than trusting a key supplied by an attacker.
"""

from __future__ import annotations

import argparse
import base64
import json

DOMAIN = b"CommonTrace public benchmark report v1\x00"


def canonical(report: dict) -> bytes:
    if not isinstance(report, dict) or report.get("public_aggregate_only") is not True:
        raise ValueError("only explicitly public aggregate reports may be signed")
    raw = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("release report exceeds 8 MiB")
    return DOMAIN + raw


def sign(report: dict, key) -> dict:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    body = canonical(report)
    return {
        "format": "commontrace-public-results-v1",
        "report": report,
        "public_key": key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex(),
        "signature": base64.b64encode(key.sign(body)).decode(),
    }


def verify(envelope: dict, *, trusted_key: str) -> dict:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if envelope.get("format") != "commontrace-public-results-v1" or envelope.get("public_key") != trusted_key:
        raise ValueError("untrusted release signer")
    try:
        public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(trusted_key))
        public.verify(base64.b64decode(envelope["signature"], validate=True), canonical(envelope["report"]))
    except (InvalidSignature, ValueError, KeyError, TypeError) as exc:
        raise ValueError("invalid release signature or manifest") from exc
    return envelope["report"]


def verify_artifacts(report: dict, root: str) -> None:
    """Check hash-bound public files after verifying the publisher signature."""
    import hashlib
    import os
    import re

    base = os.path.realpath(root)
    for name, digest in report.get("files_sha256", {}).items():
        if (
            not isinstance(name, str)
            or os.path.isabs(name)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("invalid public artifact manifest")
        filename = os.path.realpath(os.path.join(base, name))
        if os.path.commonpath([base, filename]) != base:
            raise ValueError("public artifact leaves its configured root")
        with open(filename, "rb") as stream:
            measured = (
                hashlib.file_digest(stream, "sha256").hexdigest()
                if hasattr(hashlib, "file_digest")
                else hashlib.sha256(stream.read()).hexdigest()
            )
        if measured != digest:
            raise ValueError("public artifact digest mismatch: " + name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--key", help="Ed25519 private PEM file, used only for signing")
    parser.add_argument("--out")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--trusted-key", help="Pinned public key, raw hex")
    parser.add_argument("--artifact-root", help="Verify hash-bound aggregate files under this root")
    args = parser.parse_args(argv)
    with open(args.manifest, encoding="utf-8") as stream:
        source = json.load(stream)
    if args.verify:
        if not args.trusted_key:
            parser.error("--verify requires --trusted-key")
        report = verify(source, trusted_key=args.trusted_key)
        if args.artifact_root:
            verify_artifacts(report, args.artifact_root)
        print("Release signature verified")
        return 0
    if not args.key or not args.out:
        parser.error("signing requires --key and --out")
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    with open(args.key, "rb") as stream:
        key = load_pem_private_key(stream.read(), password=None)
    with open(args.out, "w", encoding="utf-8") as stream:
        json.dump(sign(source, key), stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
