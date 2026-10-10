"""Multimodal episodes: stored as governed traces, media content-addressed and never decoded beyond its header."""
import base64
import json
import os

from benchmarks.robot_fleet_demo import _png
from commontrace import gateway, paths, trace_io

TOKEN = "e" * 40


def _post(gw, body):
    response = gw.handle("POST", "/v1/episode", {"Authorization": "Bearer " + TOKEN, "Host": "localhost"},
                         json.dumps(body).encode())
    return response.status, json.loads(response.body)


def test_episode_becomes_a_trace_with_media_and_sensors(tmp_path):
    root = str(tmp_path)
    gateway.save_config(root, gateway.GatewayConfig(env="real"))
    gw = gateway.Gateway(root, token=TOKEN)
    image = _png(20, 10, 128)
    status, out = _post(gw, {"occasion_id": "pick-1", "agent_id": "floor-01", "summary": "Dropped a mug.",
                             "sensors": {"grip_force_n": 31.5, "fragile": True, "zone": "B"},
                             "media": [{"kind": "image", "mime": "image/png", "caption": "wrist camera",
                                        "data_b64": base64.b64encode(image).decode()}],
                             "succeeded": False})
    assert status == 200, out
    [media] = out["media"]
    assert (media["width"], media["height"], media["bytes"]) == (20, 10, len(image)) and out["env"] == "real"
    assert os.path.isfile(os.path.join(paths.memory_dir(root), "media", media["sha256"] + ".png"))
    [path] = [os.path.join(paths.traces_dir(root), n) for n in os.listdir(paths.traces_dir(root))]
    trace, _ = trace_io.read(path)
    assert {"episode", "env:real", "media:image"} <= set(trace["tags"])
    assert "- fragile: true" in trace["context_text"] and "- grip_force_n: 31.5" in trace["context_text"]
    assert trace["outcome"]["resolved"] is False and trace["agent_id"] == "floor-01"
    assert trace["extensions"]["episode"]["media"][0]["sha256"] == media["sha256"]


def test_bad_episodes_are_refused(tmp_path):
    gw = gateway.Gateway(str(tmp_path), token=TOKEN)
    jpeg_claim = {"kind": "image", "mime": "image/jpeg", "data_b64": base64.b64encode(_png(4, 4, 1)).decode()}
    for body in (
        {"occasion_id": "o", "summary": ""},
        {"occasion_id": "o", "summary": "x", "media": [jpeg_claim]},  # header contradicts the mime type
        {"occasion_id": "o", "summary": "x", "media": [{"kind": "image", "mime": "image/png", "data_b64": "!!"}]},
        {"occasion_id": "o", "summary": "x", "media": [{"kind": "video", "mime": "image/png", "data_b64": "AA=="}]},
        {"occasion_id": "o", "summary": "x", "sensors": {"bad name!": 1}},
        {"occasion_id": "o", "summary": "x", "sensors": {"t": float("nan")}},
        {"occasion_id": "o", "summary": "x", "succeeded": "yes"},
    ):
        status, _out = _post(gw, body)
        assert status == 400, body
    gateway.save_config(str(tmp_path), gateway.GatewayConfig(env="sim"))
    status, _out = _post(gateway.Gateway(str(tmp_path), token=TOKEN),
                         {"occasion_id": "o", "summary": "x", "env": "real"})
    assert status == 409  # simulation and reality are never pooled
