"""Committed proof studies, canonical binary outcomes and pinned real provers."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from commontrace import frontier_protocol as fp
from commontrace import zk_effect as pure
from commontrace.runtime_cache import RuntimeCache

_CACHE: RuntimeCache[tuple[dict[str, Any], dict[str, int]]] = RuntimeCache(
    max_entries=8, max_bytes=16*fp.MAX_BYTES, ttl=60,
    weigh=lambda key, value: len(repr(key))+4096+sum(len(identity)+96 for identity in value[1]))


def _clear_cache(root: str, identities: set[str]) -> None:
    _CACHE.clear()


def _store() -> Any:
    from commontrace import nextwave_store

    nextwave_store.register_erasure_hook("effect-studies", _clear_cache)
    return nextwave_store


def _fingerprints(values: Mapping[str, dict[str, Any]]) -> dict[str, str]:
    return {identity: fp.digest(value) for identity, value in values.items()}


def _study(root: str, study: str, context: Sequence[str]) -> tuple[dict[str, Any], dict[str, int]]:
    from commontrace import memory_authority, policy_io

    store = _store()
    store.operator()
    identity = fp.digest(study)
    metadata = store.latest(root, "effect-studies").get(identity)
    if metadata is None or metadata.get("status") != "active" or not store.visible(metadata["scopes"], context):
        raise PermissionError("UniMem proof study is unavailable or outside the owner's scope")
    if (memory_authority.lineage_blocked(root, identity)
            or not policy_io.permits(root, {**metadata, "kind": "effect-studies"})):
        raise PermissionError("UniMem proof study admission is denied")
    key = (os.path.realpath(root), identity, tuple(sorted(set(context))),
        fp.checkpoint(root, store.journal_name("effect-studies")),
        fp.checkpoint(root, store.journal_name("effect-cohorts")),
        fp.checkpoint(root, store.journal_name("effect-keys")))
    def load() -> tuple[dict[str, Any], dict[str, int]]:
        header = store.get(root, "effect-studies", identity, context=context)
        entries = []
        for part in header["parts"]:
            entries.extend(store.get(root, "effect-cohorts", part, context=context)["entries"])
        selected = pure.cohort(entries)
        if pure.cohort_root(selected) != header["plan"]["cohort_root"]:
            raise PermissionError("UniMem stored proof cohort differs from its commitment")
        return header, {row["episode"]: row["propensity"] for row in selected}
    header, entries = _CACHE.get_or_load(key, load)
    current = store.admit(root, header["source_ids"], context=context, action_class="factual")
    if _fingerprints(current) != header["source_fingerprints"]:
        raise PermissionError("UniMem proof source admission or revision changed")
    return header, entries


def register(root: str, study: str, entries: list[dict[str, Any]], source_ids: list[str], *,
             context: Sequence[str] = (), alpha_ppm: int = 50_000, kind: str = "effect",
             harm_limit_ppm: int | None = None, noise_fraction: list[int] | None = None) -> dict[str, Any]:
    from commontrace import _jsonl, frontmatter, holdout_io, incident, safety, transparency_log

    store = _store()
    store.operator()
    selected = pure.cohort(entries)
    fp.count(len(source_ids), 1, 64, "proof sources")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("UniMem proof source IDs must be distinct")
    identity = fp.digest(study)
    with incident.protect(root), frontmatter.locked(holdout_io.outcomes_log_path(root)), _jsonl.locked(
            fp.filename(root, "effect-study-"+identity+".lock")):
        if identity in store.latest(root, "effect-studies"):
            header, original = _study(root, study, context)
            dp = header["plan"]["dp"]
            wanted_noise = [dp["numerator"], dp["denominator"]] if dp is not None else None
            if ({row["episode"]: row["propensity"] for row in selected} != original
                    or set(header["source_ids"]) != set(source_ids) or header["plan"]["alpha_ppm"] != alpha_ppm
                    or header["plan"]["kind"] != kind or header["plan"]["harm_limit_ppm"] != harm_limit_ppm
                    or wanted_noise != noise_fraction):
                raise PermissionError("UniMem proof study identity is immutable and cannot be rearmed")
            previous = store.latest(root, "effect-state").get(identity)
            if previous and previous.get("cohort_anchor"):
                return {"plan": header["plan"], "anchor": _cohort_receipt(root, study, header),
                        "next": "independent verifier supplies the nonce after pinning this cohort commitment"}
            fp.rows(holdout_io.outcomes_log_path(root))
            outcomes = holdout_io.read_outcomes(root)
            if any(episode in outcomes for episode in original):
                raise PermissionError("UniMem interrupted commitment recovery must precede every outcome")
            anchor = transparency_log.append(root, "effect-cohort-commitment", {
                "plan_hash": fp.digest(header["plan"]), "cohort_root": header["plan"]["cohort_root"],
                "count": header["plan"]["count"]})
            store.event(root, "effect-state", identity, {"state": "registered",
                "plan_hash": fp.digest(header["plan"]), "cohort_anchor": anchor})
            return {"plan": header["plan"], "anchor": anchor, "next": "verifier challenge required"}
        admitted = store.admit(root, source_ids, context=context, action_class="factual")
        if set(admitted) != set(source_ids):
            raise PermissionError("UniMem proof sources must be admitted canonical memories")
        if kind == "effect" and set(source_ids) & set(safety.protected(root)):
            raise PermissionError("UniMem protected hazards cannot become treatment trials")
        fp.rows(holdout_io.outcomes_log_path(root))
        outcomes = holdout_io.read_outcomes(root)
        if any(row["episode"] in outcomes for row in selected):
            raise PermissionError("UniMem the complete cohort must commit before any canonical outcomes")
        prior_keys = store.latest(root, "effect-keys").get(identity)
        if prior_keys:
            keys = store.get(root, "effect-keys", identity, context=context)
            hiding = keys["commitment_seed"]
            noise = keys["noise_seed"] or secrets.token_bytes(32).hex()
            if (keys["noise_seed"] is None) != (noise_fraction is None):
                raise PermissionError("UniMem interrupted study recovery must keep its original noise profile")
        else:
            hiding, noise = secrets.token_bytes(32).hex(), secrets.token_bytes(32).hex()
        dp = None
        if noise_fraction is not None:
            if not isinstance(noise_fraction, list) or len(noise_fraction) != 2:
                raise ValueError("UniMem finite noise fraction needs numerator and denominator")
            dp = {"numerator": noise_fraction[0], "denominator": noise_fraction[1],
                  "noise_key": pure.seed_key(noise, noise=True)}
        registered = pure.plan(study, selected, commitment_key=pure.seed_key(hiding), alpha_ppm=alpha_ppm,
            kind=kind, harm_limit_ppm=harm_limit_ppm, dp=dp)
        parts = []
        for offset in range(0, len(selected), 512):
            part = identity+"-"+str(offset//512)
            store.put(root, "effect-cohorts", part, {"entries": selected[offset:offset+512]},
                      scopes=context, sources=source_ids, immutable=True)
            parts.append(part)
        store.put(root, "effect-keys", identity, {"commitment_seed": hiding,
            "noise_seed": noise if dp is not None else None}, scopes=context, sources=source_ids, immutable=True)
        header = {"plan": registered, "parts": parts, "source_ids": source_ids,
            "source_fingerprints": _fingerprints(admitted), "registered_at": fp.now(),
            "collection": "canonical task success; harm mode uses binary task failure as its declared proxy"}
        dependencies = [store.reference("effect-cohorts", part) for part in parts]
        dependencies.append(store.reference("effect-keys", identity))
        store.put(root, "effect-studies", identity, header, scopes=context, sources=dependencies, immutable=True)
        anchor = transparency_log.append(root, "effect-cohort-commitment", {"plan_hash": fp.digest(registered),
            "cohort_root": registered["cohort_root"], "count": registered["count"]})
        store.event(root, "effect-state", identity, {"state": "registered", "plan_hash": fp.digest(registered),
                                                    "cohort_anchor": anchor})
        return {"plan": registered, "anchor": anchor,
                "next": "independent verifier supplies a fresh nonce after pinning this cohort commitment"}


def _cohort_receipt(root: str, study: str, header: Mapping[str, Any]) -> dict[str, Any]:
    from commontrace import transparency_log

    state = _store().latest(root, "effect-state").get(fp.digest(study))
    anchor = state.get("cohort_anchor") if state else None
    record = anchor.get("statement", {}).get("record", {}) if anchor else {}
    if (not anchor or not transparency_log.verify(root, anchor) or record.get("event") != "effect-cohort-commitment"
            or record.get("statement", {}).get("plan_hash") != fp.digest(header["plan"])):
        raise PermissionError("UniMem a valid cohort commitment must precede the verifier nonce")
    return dict(anchor)


def challenge(root: str, study: str, nonce: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    from commontrace import _jsonl, frontmatter, holdout_io, incident, transparency_log

    store = _store()
    store.operator()
    identity = fp.digest(study)
    with incident.protect(root), frontmatter.locked(holdout_io.outcomes_log_path(root)), _jsonl.locked(
            fp.filename(root, "effect-study-"+identity+".lock")):
        header, entries = _study(root, study, context)
        _cohort_receipt(root, study, header)
        candidate = pure.challenge(header["plan"], nonce)
        prior = store.latest(root, "effect-challenges").get(identity)
        if prior:
            if prior.get("challenge") != candidate:
                raise PermissionError("UniMem a verifier nonce cannot change after challenge enrollment")
            _challenge(root, study, header)
            return {"challenge": prior["challenge"], "anchor": prior["anchor"], "expected_hash": fp.digest(candidate)}
        fp.rows(holdout_io.outcomes_log_path(root))
        outcomes = holdout_io.read_outcomes(root)
        if any(episode in outcomes for episode in entries):
            raise PermissionError("UniMem verifier nonce must commit before every cohort outcome")
        anchor = transparency_log.append(root, "effect-verifier-challenge", {"challenge_hash": fp.digest(candidate),
                                                                          "plan_hash": candidate["plan_hash"]})
        store.event(root, "effect-challenges", identity, {"challenge": candidate, "anchor": anchor,
                                                        "enrolled_at": fp.now()})
        return {"challenge": candidate, "anchor": anchor, "expected_hash": fp.digest(candidate)}


def _challenge(root: str, study: str, header: Mapping[str, Any]) -> dict[str, str]:
    from commontrace import transparency_log

    cohort_anchor = _cohort_receipt(root, study, header)
    row = _store().latest(root, "effect-challenges").get(fp.digest(study))
    if not row or row.get("challenge", {}).get("plan_hash") != fp.digest(header["plan"]):
        raise PermissionError("UniMem a verifier challenge must precede outcome collection")
    anchored = row["anchor"].get("statement", {}).get("record", {})
    if (not transparency_log.verify(root, row["anchor"])
            or row["anchor"]["leaf"] <= cohort_anchor["leaf"]
            or anchored.get("event") != "effect-verifier-challenge"
            or anchored.get("statement", {}).get("challenge_hash") != fp.digest(row["challenge"])):
        raise PermissionError("UniMem verifier challenge transparency receipt is invalid")
    return dict(row["challenge"])


def assign(root: str, study: str, episode: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    from commontrace import safety

    header, entries = _study(root, study, context)
    if episode not in entries:
        raise PermissionError("UniMem episode is not in the complete precommitted cohort")
    if header["plan"]["kind"] == "effect" and set(header["source_ids"]) & set(safety.protected(root)):
        raise PermissionError("UniMem a newly protected source cannot enter treatment assignment")
    question = _challenge(root, study, header)
    return {"episode": episode, "assigned": pure.assignment(header["plan"], question, episode, entries[episode]),
        "propensity": entries[episode], "challenge_hash": fp.digest(question), "execution_attested": False,
        "role": "planned treatment" if header["plan"]["kind"] == "effect" else "audit partition; observes every row"}


def observe(root: str, study: str, episode: str, succeeded: bool, *, context: Sequence[str] = ()) -> dict[str, Any]:
    from commontrace import holdout_io, incident

    with incident.protect(root):
        planned = assign(root, study, episode, context=context)
        created = holdout_io.record_outcome(root, episode, succeeded)
        return {"episode": episode, "recorded": created, "assignment": planned,
                "evidence": "canonical outcome reporter assertion; execution not attested"}


def _witness(root: str, study: str, context: Sequence[str]) -> dict[str, Any]:
    from commontrace import frontmatter, holdout_io

    header, entries = _study(root, study, context)
    question = _challenge(root, study, header)
    with frontmatter.locked(holdout_io.outcomes_log_path(root)):
        fp.rows(holdout_io.outcomes_log_path(root))
        outcomes = holdout_io.read_outcomes(root)
        if any(episode not in outcomes for episode in entries):
            raise PermissionError("UniMem effect proofs require every preregistered canonical outcome")
        rows = [{"episode": episode, "propensity": propensity,
            "assigned": pure.assignment(header["plan"], question, episode, propensity),
            "outcome": pure.SCALE*int(not outcomes[episode] if header["plan"]["kind"] == "harm" else outcomes[episode])}
            for episode, propensity in sorted(entries.items())]
    keys = _store().get(root, "effect-keys", fp.digest(study), context=context)
    return {"plan": header["plan"], "challenge": question, "rows": rows, **keys}


def _binary_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(program: str, action: str, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
    import subprocess

    raw = json.dumps(request, separators=(",", ":"), allow_nan=False).encode() if request is not None else b""
    if len(raw) > 16*fp.MAX_BYTES:
        raise ValueError("UniMem native proof request exceeds 16 MiB")
    env = dict(os.environ, RISC0_PROVER="local")
    env.pop("RISC0_DEV_MODE", None)
    try:
        result = subprocess.run([program, action], input=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=600, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise pure.BackendUnavailable("UniMem native proof program is unavailable or exceeded its time limit") from None
    if result.returncode and action == "verify":
        return {"verified": False}
    if result.returncode or len(result.stdout) > 16*fp.MAX_BYTES:
        raise pure.BackendUnavailable("UniMem native proof program failed; no replacement certificate was issued")
    decoded = json.loads(result.stdout)
    if not isinstance(decoded, dict):
        raise ValueError("UniMem native prover returned a non-object")
    return decoded


def configure_prover(root: str, program: str, image_id: str) -> dict[str, Any]:
    from commontrace import _jsonl

    store = _store()
    store.operator()
    pure._hex(image_id)
    executable = Path(program)
    if not executable.is_absolute() or executable.is_symlink() or not executable.is_file():
        raise ValueError("UniMem a native prover needs an explicit regular absolute binary path")
    if not os.access(executable, os.X_OK) or executable.stat().st_mode & 0o022:
        raise PermissionError("UniMem native prover must be executable and not writable by another user or group")
    digest = _binary_digest(executable)
    descriptor = _run(str(executable), "describe")
    if (descriptor.get("schema") != "unimem.risc0-backend.v1" or descriptor.get("sdk") != "3.0.6"
            or descriptor.get("development_receipts") is not False
            or descriptor.get("image_id") not in {None, image_id}):
        raise PermissionError("UniMem native prover descriptor does not match the pinned receipt profile")
    body = {"schema": "unimem.effect-prover.v1", "program": str(executable), "binary_digest": digest,
            "image_id": image_id, "proving": descriptor.get("proving") is True, "sdk": "3.0.6"}
    filename = fp.filename(root, "effect-prover.json")
    with _jsonl.locked(filename):
        if Path(filename).exists():
            if _prover(root) != body:
                raise PermissionError(
                    "UniMem receipt verifier pins are immutable; enroll a distinct store for rotation")
        else:
            _jsonl.write_json(filename, fp.sign(root, body))
    return body


def _prover(root: str) -> dict[str, Any]:
    filename = fp.filename(root, "effect-prover.json")
    try:
        with open(filename, "rb") as stream:
            raw = stream.read(fp.MAX_BYTES+1)
    except FileNotFoundError:
        raise pure.BackendUnavailable("UniMem configure a pinned RISC Zero host built with the zk toolchain") from None
    if len(raw) > fp.MAX_BYTES:
        raise PermissionError("UniMem prover enrollment exceeds its bound")
    signed = json.loads(raw)
    if not fp.verify(root, signed) or signed["record"].get("schema") != "unimem.effect-prover.v1":
        raise PermissionError("UniMem receipt verifier enrollment is not authentic")
    body = dict(signed["record"])
    if _binary_digest(Path(body["program"])) != body["binary_digest"]:
        raise PermissionError("UniMem receipt verifier binary differs from its operator pin")
    return body


def _risc0_verify(root: str, proof: Mapping[str, Any], expected_challenge: str) -> bool:
    settings = _prover(root)
    if (set(proof) != {"backend", "receipt", "statement", "image_id", "private_rows"}
            or proof.get("backend") != "risc0" or proof.get("image_id") != settings["image_id"]
            or proof.get("private_rows") is not True):
        return False
    result = _run(settings["program"], "verify", {"receipt": proof["receipt"], "image_id": settings["image_id"],
        "expected_challenge": pure._hex(expected_challenge), "expected_statement": proof["statement"]})
    return result.get("verified") is True and result.get("statement") == proof["statement"]


def prove(root: str, study: str, *, backend: str = "transparent", context: Sequence[str] = ()) -> dict[str, Any]:
    from commontrace import _jsonl, incident, policy_io, sentinel_io, transparency_log

    store = _store()
    store.operator()
    identity = fp.digest(study)
    with incident.protect(root), _jsonl.locked(fp.filename(root, "effect-study-"+identity+".lock")):
        header, _ = _study(root, study, context)
        policy_io.assert_export(root, header["source_ids"], context=list(context))
        sentinel_io.assert_generalize(root, header["source_ids"], context=context)
        prior = store.latest(root, "effect-proofs").get(identity)
        if prior:
            envelope = store.get(root, "effect-proofs", identity, context=context)
            if envelope["backend"] != backend:
                raise PermissionError("UniMem a committed cohort permits only its original public release backend")
            raw = "".join(store.get(root, "effect-proof-parts", part, context=context)["data"]
                          for part in envelope["parts"])
            if hashlib.sha256(raw.encode()).hexdigest() != envelope["proof_digest"]:
                raise PermissionError("UniMem stored proof differs from its immutable commitment")
            proof = json.loads(raw)
            anchor = transparency_log.append(root, "effect-statement", {
                "statement_digest": fp.digest(proof["statement"]), "proof_digest": envelope["proof_digest"],
                "backend": backend})
            return {"proof": proof, "anchor": anchor,
                    "expected_challenge": fp.digest(_challenge(root, study, header)),
                    "collection": header["collection"]}
        witness = _witness(root, study, context)
        if backend == "transparent":
            proof = pure.transparent(witness)
        elif backend == "risc0":
            settings = _prover(root)
            if not settings["proving"]:
                raise pure.BackendUnavailable(
                    "UniMem this pinned host verifies receipts but has no zkVM proving toolchain")
            proof = _run(settings["program"], "prove", witness)
            if not _risc0_verify(root, proof, fp.digest(witness["challenge"])):
                raise PermissionError("UniMem native proof failed independent receipt verification")
            if proof.get("statement") != pure.statement(witness):
                raise PermissionError("UniMem native guest journal differs from complete integer recomputation")
        elif backend == "chunked":
            raise pure.BackendUnavailable(
                "UniMem chunked circuits require an operator-provisioned recursive proof adapter")
        else:
            raise ValueError("UniMem unknown effect proof backend")
        raw = json.dumps(proof, sort_keys=True, separators=(",", ":"), allow_nan=False)
        parts = []
        content_digest = hashlib.sha256(raw.encode()).hexdigest()
        for offset in range(0, len(raw), 65536):
            key = content_digest+"-"+str(offset//65536)
            store.put(root, "effect-proof-parts", key, {"data": raw[offset:offset+65536]}, scopes=context,
                      sources=[store.reference("effect-studies", identity)], immutable=True)
            parts.append(key)
        envelope = {"statement": proof["statement"], "backend": backend, "parts": parts,
                    "proof_digest": hashlib.sha256(raw.encode()).hexdigest()}
        store.put(root, "effect-proofs", identity, envelope, scopes=context,
            sources=[store.reference("effect-proof-parts", key) for key in parts], immutable=True)
        anchor = transparency_log.append(root, "effect-statement", {"statement_digest": fp.digest(proof["statement"]),
            "proof_digest": envelope["proof_digest"], "backend": backend})
        return {"proof": proof, "anchor": anchor, "expected_challenge": fp.digest(witness["challenge"]),
                "collection": header["collection"]}


def verify(root: str, proof: dict[str, Any], expected_challenge: str, *,
           witness: dict[str, Any] | None = None) -> dict[str, Any]:
    if proof.get("backend") == "transparent":
        valid = witness is not None and pure.verify_transparent(proof, witness, expected_challenge)
    elif proof.get("backend") == "risc0":
        valid = _risc0_verify(root, proof, expected_challenge)
    else:
        raise pure.BackendUnavailable("UniMem this verification requires the registered proof backend")
    return {"verified": bool(valid), "backend": proof.get("backend"), "execution_attested": False}


def export_witness(root: str, study: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    """Explicit owner disclosure for transparent verification, never an agent tool."""
    _store().operator()
    return _witness(root, study, context)
