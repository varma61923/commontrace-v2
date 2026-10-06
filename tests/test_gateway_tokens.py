from __future__ import annotations

import json
import multiprocessing
import os
import stat
from concurrent.futures import ProcessPoolExecutor

import pytest

from commontrace import gateway, paths
from commontrace import gateway_tokens as tokens


def _create(root):
    return tokens.load_or_create_token(root)


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="requires fork")
def test_first_start_is_atomic_across_processes(tmp_path):
    root = str(tmp_path)
    with ProcessPoolExecutor(6, mp_context=multiprocessing.get_context("fork")) as pool:
        values = list(pool.map(_create, [root] * 24))
    assert len(set(values)) == 1
    assert json.loads(open(tokens.token_path(root)).read())["token"] == values[0]
    assert stat.S_IMODE(os.stat(tokens.token_path(root)).st_mode) == 0o600


def test_rotation_revocation_and_expiry_reach_an_existing_gateway(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(tokens.time, "time", lambda: clock[0])
    root = str(tmp_path)
    first = tokens.load_or_create_token(root, ttl=10)
    app = gateway.Gateway(root, token=first, token_provider=tokens.FileTokenProvider(root))
    def auth(value):
        return app._authorised({"Authorization": f"Bearer {value}"})
    assert auth(first)
    second = tokens.rotate_token(root, ttl=20)
    assert not auth(first) and auth(second)
    clock[0] += 20
    assert not auth(second)
    third = tokens.rotate_token(root)
    assert auth(third)
    tokens.revoke_token(root)
    assert not auth(third)
    with pytest.raises(ValueError, match="revoked"):
        tokens.load_or_create_token(root)
    assert auth(tokens.rotate_token(root))


def test_plaintext_legacy_token_and_permission_repair(tmp_path):
    root = str(tmp_path)
    os.makedirs(paths.memory_dir(root))
    path = tokens.token_path(root)
    value = "legacy" * 8
    with open(path, "w") as fh:
        fh.write(value + "\n")
    os.chmod(path, 0o644)
    assert tokens.load_or_create_token(root) == tokens.FileTokenProvider(root)() == value
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_identity_follows_an_injected_live_provider_without_a_static_token(tmp_path):
    root = str(tmp_path)
    first = tokens.load_or_create_token(root)
    app = gateway.Gateway(root, token_provider=tokens.FileTokenProvider(root))
    assert app._authorised({"Authorization": f"Bearer {first}"})
    assert app._whoami({}, {})["authenticated"] is True
    second = tokens.rotate_token(root)
    assert app._whoami({}, {})["token_prefix"] == second[:8] + "..."


def test_bad_missing_and_symlink_credentials_fail_closed(tmp_path):
    root = str(tmp_path)
    tokens.load_or_create_token(root)
    provider = tokens.FileTokenProvider(root)
    assert provider()
    path = tokens.token_path(root)
    with open(path, "w") as fh:
        fh.write('{"version": 1, "token": "bad"}')
    assert provider() is None
    with pytest.raises(ValueError):
        tokens.load_or_create_token(root)
    os.unlink(path)
    assert provider() is None
    outside = tmp_path / "outside"
    outside.write_text("x" * 40)
    os.symlink(outside, path)
    assert provider() is None
    with pytest.raises(OSError):
        tokens.rotate_token(root)
    assert outside.read_text() == "x" * 40


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan")])
def test_invalid_lifetimes_do_not_write_credentials(tmp_path, ttl):
    with pytest.raises(ValueError):
        tokens.load_or_create_token(str(tmp_path), ttl=ttl)
    assert not os.path.exists(tokens.token_path(str(tmp_path)))
