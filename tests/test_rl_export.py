"""UniMem causal broker tests: label isolation, unbiased DR labels and erasure."""
from __future__ import annotations

import copy
import hashlib
import json
import random
from pathlib import Path

import pytest

from commontrace import frontmatter, hierarchical, holdout_io, paths, privacy_erasure, rl_io
from commontrace import nextwave_store as store
from commontrace import rl_export as rl


def episode(i, *, treated=True, outcome=True, scopes=(), rate=.5):
    return {"occasion_id": str(i), "trace_id": "trace-"+str(i), "unit": "memory", "cluster": "release",
            "context": "coding", "prompt": "Check this release.", "response": "Checked" if outcome else "Unchecked",
            "scopes": list(scopes), "integrity": "operator", "confidentiality": "team",
            "injected": treated, "outcome": outcome, "rate": rate}


def seed(root, n=16, *, context=(), authenticated=False, response_suffix=""):
    if authenticated:
        fact, _ = hierarchical.append_facts(root, [{"statement": "Check deployment signatures before release.",
                                                  "scopes": list(context)}])[0]
    else:
        fact, _ = hierarchical.add_fact(root, "Check deployment signatures before release.", scopes=list(context))
    holdout_io.configure(root, rate=.5, salt="rl-cohort")
    observations = []
    for i in range(n):
        occasion = "occasion-" + str(i)
        withheld = holdout_io.assign_and_log(root, [fact.id], occasion_id=occasion, rate=.5, salt="rl-cohort",
                                             context="coding", revisions={fact.id: fact.revision})
        outcome = fact.id not in withheld
        holdout_io.record_outcome(root, occasion, outcome)
        observations.append((occasion, outcome))
    captures = []
    for occasion, outcome in observations:
        captures.append(rl_io.capture(root, occasion, fact.id, "release", "Check this release.",
                        ("Checked" if outcome else "Unchecked") + response_suffix, context=context))
    return fact, captures


def test_seeded_dr_advantage_and_effect_are_unbiased_under_changing_propensities():
    rng = random.Random(211)
    improvement, effects = [], []
    for repeat in range(80):
        labels = []
        for i in range(300):
            p = .85 if i < 150 else .15
            base = .2 if i < 150 else .6
            z = rng.random() < p
            y = rng.random() < base + .2*z
            labels.append(rl.advantage(episode(i, treated=z, outcome=y, rate=1-p)))
        improvement.append(sum(x["advantage"] for x in labels)/300)
        effects.append(sum(x["aipw_effect_score"] for x in labels)/300)
    assert sum(improvement)/80 == pytest.approx(.1, abs=.018)
    assert sum(effects)/80 == pytest.approx(.2, abs=.025)


def test_dpo_pairs_never_cross_label_prompt_cluster_or_context_boundaries():
    episodes = [episode("a", scopes=["team:a"]), episode("b", treated=False, outcome=False, scopes=["team:b"])]
    measured = {"memory": {"effect": .2, "interval": [-.1, .5]}}
    assert rl.preferences(episodes, measured) == []
    episodes[1]["scopes"] = ["team:a"]
    pair = rl.preferences(episodes, measured)[0]
    assert pair["meta"]["same_instance_counterfactual"] is False
    assert pair["meta"]["population_effect"] == .2
    assert pair["meta"]["kl_to_base"][0]["status"] == "unchecked"
    for field in ("integrity", "confidentiality", "cluster", "context", "prompt"):
        changed = copy.deepcopy(episodes)
        changed[1][field] = "different"
        assert rl.preferences(changed, measured) == []
    with pytest.raises(ValueError, match="distinct"):
        rl.preferences(episodes + episodes, measured)


def test_replay_weights_and_sampled_kl_are_honest_and_bounded():
    items = [episode(i) for i in range(4)]
    items[-1]["unit"] = "other"
    intervals = {"memory": [.2, .8], "other": [-.8, -.2]}
    weights = rl.priorities(items, intervals)
    assert sum(w["probability"] for w in weights) == pytest.approx(1)
    assert weights[0]["probability"] > weights[-1]["probability"]
    assert len(rl.sample(items, intervals, count=20, seed=2)) == 20
    assert rl.token_kl(None, None, .1)["status"] == "unchecked"
    assert rl.token_kl([-.2], [-.1], .1)["estimate"] == pytest.approx(-.1)
    assert rl.token_kl([-.1], [-2], .1)["status"] == "over sampled budget"
    for bad in (float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            rl.token_kl([bad], [-1], .1)


def test_complete_authenticated_cohort_export_and_later_erasure(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda fd: None)
    root = str(tmp_path)
    fact, captures = seed(root, context=["team:a"])
    exported = rl_io.prepare(root, [fact.id], context=["team:a"])
    raw = b"".join(rl_io.lines(root, exported["id"], context=["team:a"]))
    records = [json.loads(line) for line in raw.splitlines()]
    assert records and exported["row_count"] == len(records)
    assert hashlib.sha256(raw).hexdigest() == exported["manifest_digest"]
    assert rl_io.status(root, exported["id"], context=["team:a"])["status"] == "active"
    with pytest.raises(PermissionError):
        rl_io.prepare(root, [fact.id], context=["team:b"])
    privacy_erasure.purge_fact(root, fact.id, reason="owner request")
    assert rl_io.status(root, exported["id"], context=["team:a"])["status"] == "invalidated"
    assert not store.traces(root)
    with pytest.raises((PermissionError, ValueError)):
        list(rl_io.lines(root, exported["id"], context=["team:a"]))
    assert captures


def test_tampered_trace_binding_and_missing_responses_refuse_export(tmp_path, monkeypatch):
    monkeypatch.setattr("os.fsync", lambda fd: None)
    root = str(tmp_path)
    fact, _ = seed(root)
    path = next(Path(paths.traces_dir(root)).glob("*.md"))
    fm, body = frontmatter.read(str(path))
    fm["extensions"]["profile"]["procedure"]["rl"]["cluster"] = "laundered"
    frontmatter.write(str(path), fm, body)
    with pytest.raises(PermissionError, match="complete"):
        rl_io.prepare(root, [fact.id])


def test_qualified_artifact_sources_validate_scope_cycle_and_erasure(tmp_path):
    root = str(tmp_path)
    fact, _ = hierarchical.add_fact(root, "private scoped signature evidence", scopes=["team:a"])
    store.put(root, "test-parts", "part", {"text": "payload"}, scopes=["team:a"], sources=[fact.id])
    store.put(root, "test-manifests", "manifest", {"part": "part"}, scopes=["team:a"],
              sources=[store.reference("test-parts", "part")])
    assert store.get(root, "test-manifests", "manifest", context=["team:a"])["part"] == "part"
    with pytest.raises(PermissionError):
        store.get(root, "test-manifests", "manifest", context=["team:b"])
    store.put(root, "cyclic", "a", {}, sources=[store.reference("cyclic", "b")])
    store.put(root, "cyclic", "b", {}, sources=[store.reference("cyclic", "a")])
    with pytest.raises(ValueError, match="cycle"):
        store.get(root, "cyclic", "a")
    store.erase(root, [fact.id])
    assert store.latest(root, "test-manifests")["manifest"]["status"] == "erased"


def test_stream_preserves_authorization_context_and_aborts_after_erasure(tmp_path, monkeypatch):
    from commontrace import gateway, memory_control

    token = memory_control.REQUIRED_PRINCIPAL_SCOPE.set("container:a")
    try:
        deferred = gateway.Response.streaming(200, (memory_control.REQUIRED_PRINCIPAL_SCOPE.get().encode()
                                      for _ in range(1)), content_type="text/plain")
    finally:
        memory_control.REQUIRED_PRINCIPAL_SCOPE.reset(token)
    assert b"".join(deferred.stream) == b"container:a"
    monkeypatch.setattr("os.fsync", lambda fd: None)
    root = str(tmp_path)
    fact, _ = seed(root, n=70)
    manifest = rl_io.prepare(root, [fact.id], format="advantage")
    stream = rl_io.lines(root, manifest["id"])
    assert next(stream)
    privacy_erasure.purge_fact(root, fact.id, reason="owner request")
    with pytest.raises((PermissionError, ValueError)):
        next(stream)


def test_http_and_cli_export_use_the_real_wire_format(tmp_path, monkeypatch, capsys):
    import argparse
    import threading
    import urllib.error
    import urllib.request

    from commontrace import gateway
    from commontrace.commands import nextwave

    monkeypatch.setattr("os.fsync", lambda fd: None)
    root = str(tmp_path)
    fact, _ = seed(root, context=["container:a"])
    app = gateway.Gateway(root, token="test-root-token")
    server = gateway.make_http_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1/experiment/export-rl"
    body = {"units": [fact.id], "format": "advantage", "context": ["container:a"]}
    try:
        request = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": "Bearer test-root-token", "X-Container-Tag": "a"})
        with urllib.request.urlopen(request, timeout=5) as response:
            wire = response.read()
            assert response.headers["Transfer-Encoding"] == "chunked"
            assert hashlib.sha256(wire).hexdigest() == response.headers["X-UniMem-Manifest"]
        assert len(wire.splitlines()) == 16
        request = urllib.request.Request(endpoint, data=json.dumps({**body, "format": "invalid"}).encode(),
                         headers={"Content-Type": "application/json", "Authorization": "Bearer test-root-token"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 400
        request = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": "Bearer test-root-token", "X-Container-Tag": "b"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    args = argparse.Namespace(request=json.dumps({"units": [fact.id], "context": ["container:a"]}),
                              format="advantage", dest=root)
    assert nextwave.run_rl(args) == 0
    cli_wire = capsys.readouterr().out
    assert len(cli_wire.splitlines()) == 16 and json.loads(cli_wire.splitlines()[0])["meta"]["unit"] == fact.id


@pytest.mark.parametrize("format,connector", [("dpo", "generic"), ("advantage", "agent-lightning"), ("replay", "grader")])
def test_cli_parser_emits_all_formats(tmp_path, monkeypatch, capsys, format, connector):
    from commontrace import cli

    monkeypatch.setattr("os.fsync", lambda fd: None)
    root = str(tmp_path)
    fact, _ = seed(root)
    request = json.dumps({"units": [fact.id], "connector": connector})
    assert cli.main(["experiment", "export-rl", request, "--format", format, "--dest", root]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows
    if format == "dpo":
        assert {"prompt", "chosen", "rejected", "meta"} <= rows[0].keys()
    elif connector == "agent-lightning":
        assert "training_record" in rows[0]
    else:
        assert "sample" in rows[0]


def test_authenticated_scope_label_lists_and_egress_canaries(tmp_path, monkeypatch):
    from commontrace import memory_ifc

    monkeypatch.setattr("os.fsync", lambda _: None)
    root = str(tmp_path / "valid")
    fact, _ = seed(root, context=["team:a"], authenticated=True)
    manifest = rl_io.prepare(root, [fact.id], context=["team:a"])
    rows = [json.loads(line) for line in b"".join(rl_io.lines(root, manifest["id"], context=["team:a"])).splitlines()]
    assert rows and rows[0]["meta"]["confidentiality"] == ["team:a"]
    other = str(tmp_path / "canary")
    token = memory_ifc.create_canary(other, "tenant-a")["text"]
    fact, _ = seed(other, response_suffix=" " + token)
    with pytest.raises(PermissionError, match="egress"):
        rl_io.prepare(other, [fact.id], format="advantage")
