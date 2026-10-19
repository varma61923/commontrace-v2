"""Subject-key crypto-shredding and bounded live-erasure certificates.

Membership-inference probing is inspired by arXiv:2405.20446, but a finite probe
is a diagnostic, not a proof of privacy. Crypto-shredding covers envelopes sealed
before backup when subject wrapping keys are outside backups and irreversibly
destroyed across all vault replicas. It cannot retroactively encrypt plaintext
backups. The filesystem vault is a reference backend; production deployments
must enforce the same destruction boundary in their KMS.
"""
from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Sequence
from typing import Any

from commontrace import _jsonl, incident, lineage, memory_authority, paths, safety
from commontrace import frontier_protocol as fp
from commontrace import transparency_log as log


def _aes() -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise RuntimeError("subject-key encryption requires commontrace[security]") from None
    return AESGCM


def _sync_directory(directory: str) -> None:
    if os.name == "posix":
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class SubjectVault:
    """Non-exported subject wrapping keys, isolated from backed-up content."""

    def __init__(self, store_root: str, directory: str):
        self.root, self.directory = os.path.realpath(store_root), os.path.realpath(directory)
        if paths.is_within_directory(self.root, self.directory):
            raise ValueError("subject-key vault must be outside the content store and its backups")
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        self.tombstones = os.path.join(self.directory, "destroyed.jsonl")

    def reference(self, subject: str) -> str:
        return fp.digest({"store": self.root, "subject": fp.text(subject, "subject", 1024)})

    def _key(self, subject: str, *, create: bool = False) -> bytes:
        reference = self.reference(subject)
        path = os.path.join(self.directory, reference + ".key")
        with _jsonl.locked(self.tombstones):
            if any(row["reference"] == reference for row in fp.rows(self.tombstones)):
                raise PermissionError("subject key was destroyed")
            if os.path.islink(path):
                raise ValueError("subject key must not be a symlink")
            if create:
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(secrets.token_bytes(32))
                        stream.flush()
                        os.fsync(stream.fileno())
                    _sync_directory(self.directory)
            try:
                with open(path, "rb") as stream:
                    key = stream.read(33)
            except FileNotFoundError:
                raise PermissionError("subject wrapping key is unavailable") from None
            if len(key) != 32:
                raise ValueError("invalid subject wrapping key")
            return key

    def seal(self, subject: str, record_id: str, plaintext: str, *,
             key_wrapper: Callable[[str], str] | None = None) -> dict[str, Any]:
        fp.text(record_id)
        fp.text(plaintext, "subject content", 64_000)
        key = self._key(subject, create=True)
        dek, nonce, wrap_nonce = secrets.token_bytes(32), secrets.token_bytes(12), secrets.token_bytes(12)
        reference = self.reference(subject)
        aad = fp.wire({"reference": reference, "record_id": record_id})
        encoded_key = key_wrapper(dek.hex()) if key_wrapper else dek.hex()
        return {"schema": "subject-envelope-v1", "reference": reference, "record_id": record_id,
                "ciphertext": _aes()(dek).encrypt(nonce, plaintext.encode(), aad).hex(), "nonce": nonce.hex(),
                "wrapped_key": _aes()(key).encrypt(wrap_nonce, encoded_key.encode(), aad).hex(),
                "wrap_nonce": wrap_nonce.hex(), "outer_wrapped": key_wrapper is not None}

    def open(self, subject: str, envelope: dict[str, Any], *,
             key_unwrapper: Callable[[str], str] | None = None) -> str:
        fp.wire(envelope)
        if envelope.get("schema") != "subject-envelope-v1" or envelope.get("reference") != self.reference(subject):
            raise PermissionError("subject envelope reference mismatch")
        key = self._key(subject)
        aad = fp.wire({"reference": envelope["reference"], "record_id": envelope["record_id"]})
        try:
            encoded_key = _aes()(key).decrypt(bytes.fromhex(envelope["wrap_nonce"]),
                                             bytes.fromhex(envelope["wrapped_key"]), aad).decode()
            if envelope.get("outer_wrapped"):
                if key_unwrapper is None:
                    raise ValueError("configured outer envelope key is required")
                encoded_key = key_unwrapper(encoded_key)
            dek = bytes.fromhex(encoded_key)
            return str(_aes()(dek).decrypt(bytes.fromhex(envelope["nonce"]),
                                           bytes.fromhex(envelope["ciphertext"]), aad).decode())
        except Exception as exc:
            raise ValueError("subject envelope authentication failed") from exc

    def destroy(self, subject: str) -> dict[str, Any]:
        reference = self.reference(subject)
        with _jsonl.locked(self.tombstones):
            prior = [row for row in fp.rows(self.tombstones) if row["reference"] == reference]
            path = os.path.join(self.directory, reference + ".key")
            if os.path.islink(path):
                raise ValueError("subject key must not be a symlink")
            if not prior and not os.path.exists(path):
                raise ValueError("cannot attest destruction of an unknown subject key")
            tombstone = prior[0] if prior else {
                "reference": reference, "destroyed_at": fp.now(), "kind": "filesystem-vault-key-destruction"}
            if not prior:
                # Commit non-reuse before deleting. A crash cannot recreate the
                # same reference; retry completes unlink before returning proof.
                _jsonl.append_row(self.tombstones, tombstone)
                with open(self.tombstones, "rb") as stream:
                    os.fsync(stream.fileno())
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            _sync_directory(self.directory)
            return tombstone


def probe(retrieve: Callable[[str], Sequence[dict[str, Any]]], queries: Sequence[str], *,
          forbidden: Sequence[str], threshold: float = 0) -> dict[str, Any]:
    if (not isinstance(queries, Sequence) or isinstance(queries, (str, bytes))
            or not isinstance(forbidden, Sequence) or isinstance(forbidden, (str, bytes))):
        raise ValueError("probe queries and forbidden tokens must be sequences of strings")
    fp.count(len(queries), 1, 128)
    fp.count(len(forbidden), 1, 256)
    fp.number(threshold, 0, 1)
    needles = [fp.text(value, "probe token", 4096).casefold() for value in forbidden]
    hits = 0
    for query in queries:
        rows = retrieve(fp.text(query, "probe query", 2000))
        fp.count(len(rows), 0, 1000)
        hits += any(needle in str(row.get("text", "")).casefold() for row in rows for needle in needles)
    rate = hits / len(queries)
    return {"passed": rate <= threshold, "hit_rate": rate, "queries": len(queries), "threshold": threshold,
            "kind": "bounded live retrieval diagnostic; not a universal membership-inference guarantee"}


def erase(root: str, subject: str, identities: Sequence[str], vault: SubjectVault, *, reason: str,
          retrieval_probe: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Revoke descendants, purge canonical facts and shred the subject wrapping key.

    Certificates explicitly scope backup claims to pre-encrypted envelopes.
    Unencrypted history and remote stores require their own deletion adapters.
    """
    from commontrace import hierarchical, lesson_admission, lesson_io, privacy_erasure

    if not memory_authority.is_operator():
        raise PermissionError("certified erasure requires an operator")
    fp.text(reason, "erasure reason")
    fp.text(subject, "subject", 1024)
    if vault.root != os.path.realpath(root):
        raise ValueError("subject-key vault is bound to a different content store")
    if not isinstance(identities, Sequence) or isinstance(identities, (str, bytes)):
        raise ValueError("erasure identities must be a sequence of memory identifiers")
    fp.count(len(identities), 1, 256)
    deleted = {fp.text(identity) for identity in identities}
    for identity in identities:
        deleted.update(lineage.descendants(root, identity))
    fp.count(len(deleted), 1, 1024, "erasure cascade")
    vault._key(subject)  # Validate the destruction capability before mutating content.
    with incident.protect(root):
        incident.assert_unpinned(root, list(deleted))
        safety.assert_mutable(root, list(deleted))
        # Revoke admission before destroying keys or touching projections.
        for identity in sorted(deleted):
            memory_authority.record_forgetting(root, identity, forgotten=True)
        for identity in sorted(deleted):
            if identity in hierarchical.load_facts(root):
                privacy_erasure.purge_fact(root, identity, reason=reason)
            path = lesson_io.lesson_path(root, identity)
            if path:
                lesson_admission.revoke(root, path, actor="subject-erasure")
                os.unlink(path)
        history_path = lesson_io.revisions_log_path(root)
        with _jsonl.locked(history_path):
            history = fp.rows(history_path)
            _jsonl.write_rows(history_path, [row for row in history
                              if lesson_io.canonical_slug(str(row.get("lesson", ""))) not in deleted])
        privacy_erasure._purge_mirrors(root, deleted)
        from commontrace import nextwave_store

        managed_deleted = nextwave_store.erase(root, sorted(deleted))
        destruction = vault.destroy(subject)
        result = retrieval_probe() if retrieval_probe else {"passed": False, "status": "not_run"}
        body = {"subject_tombstone": vault.reference(subject), "deleted_ids": sorted(deleted),
                "key_destruction": destruction, "probe": result, "issued_at": fp.now(),
                "backup_scope": "pre-encrypted subject envelopes; vault keys and plaintext backups excluded",
                "remote_erasure": False, "reason_hash": fp.digest(fp.text(reason)),
                "kind": "conditional crypto-erasure certificate; probe status is explicit"}
        if managed_deleted:
            body["deleted_managed_artifacts"] = managed_deleted
        signed = fp.sign(root, body)
        anchor = log.append(root, "certified-erasure", {"certificate_digest": fp.digest(signed)})
        fp.append(root, "deletion-certificates.jsonl", {"certificate": signed, "anchor": anchor})
        return {"certificate": signed, "anchor": anchor}


def verify(root: str, certificate: dict[str, Any]) -> bool:
    try:
        return (fp.verify(root, certificate["certificate"]) and log.verify(root, certificate["anchor"])
                and certificate["anchor"]["statement"]["record"]["statement"]["certificate_digest"]
                == fp.digest(certificate["certificate"]))
    except (ValueError, KeyError, TypeError):
        return False


def verify_bytes(
    root: str, identities: Sequence[str], *, strings: Sequence[str], embeddings: Sequence[str] = (),
    ngram: int = 32, vacuum: bool = True,
) -> dict[str, Any]:
    """Issue a separate conditional byte receipt after canonical erasure.

    Existing erasure certificate bodies remain unchanged. The byte verifier
    requires automatic local-store inventory and all tracked downstream ACKs;
    opaque stores and uninspected remote replicas remain unverified.
    """
    from commontrace import erasure_verify

    return erasure_verify.verify(root, identities, strings=strings, embeddings=embeddings, ngram=ngram, vacuum=vacuum)
