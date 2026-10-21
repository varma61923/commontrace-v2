"""Exercise actual Rust arithmetic and SDK verification when native binaries exist."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from unimem import frontier_protocol as fp
from unimem import zk_effect, zk_effect_io
from tests.test_zk_effect import witness


def program(name):
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is unset: build the optional Rust/RISC Zero toolchain first")
    return value


@pytest.mark.parametrize("kind,private", [("effect", False), ("harm", False), ("effect", True)])
def test_real_rust_arithmetic_matches_python(kind, private):
    executable = program("UNIMEM_EFFECT_CORE")
    observed = witness(2400 if private else 200, kind=kind, dp=private)
    result = subprocess.run([executable], input=json.dumps(observed), capture_output=True,
                            text=True, timeout=60, check=True)
    assert json.loads(result.stdout) == zk_effect.statement(observed)
    observed["rows"][0]["assigned"] = not observed["rows"][0]["assigned"]
    result = subprocess.run([executable], input=json.dumps(observed), capture_output=True,
                            text=True, timeout=60, check=False)
    assert result.returncode != 0


def test_actual_sdk_host_pins_binary_and_rejects_invalid_receipts(tmp_path):
    executable = program("UNIMEM_EFFECT_HOST")
    descriptor = zk_effect_io._run(executable, "describe")
    assert descriptor["development_receipts"] is False
    image = descriptor["image_id"] or "00"*32
    root = str(tmp_path)
    pinned = zk_effect_io.configure_prover(root, executable, image)
    assert pinned["sdk"] == "3.0.6"
    assert zk_effect_io.configure_prover(root, executable, image) == pinned
    forged = {"backend": "risc0", "receipt": {}, "image_id": image,
              "statement": zk_effect.statement(witness()), "private_rows": True}
    assert not zk_effect_io.verify(root, forged, "ab"*32)["verified"]
    forged["image_id"] = "ff"*32
    assert not zk_effect_io.verify(root, forged, "ab"*32)["verified"]


def test_real_zkvm_receipt_roundtrip_and_altered_statement(tmp_path):
    executable = program("UNIMEM_EFFECT_HOST")
    descriptor = zk_effect_io._run(executable, "describe")
    if not descriptor["proving"]:
        pytest.skip("RISC Zero host is verifier-only; custom guest toolchain and --features prove are required")
    root = str(tmp_path)
    zk_effect_io.configure_prover(root, executable, descriptor["image_id"])
    observed = witness(4)
    expected = fp.digest(observed["challenge"])
    proof = zk_effect_io._run(executable, "prove", observed)
    assert proof["statement"] == zk_effect.statement(observed)
    assert zk_effect_io.verify(root, proof, expected)["verified"]
    assert not zk_effect_io.verify(root, proof, "ff"*32)["verified"]
    proof["statement"]["lower_ppm"] += 1
    assert not zk_effect_io.verify(root, proof, expected)["verified"]
