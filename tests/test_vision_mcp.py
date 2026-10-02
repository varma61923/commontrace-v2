from __future__ import annotations

import asyncio
import base64
import io
import json

import pytest

from commontrace import llm, mcp_tools, vision


def _payload(result) -> dict:
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


def _call(server, name: str, **arguments) -> dict:
    return _payload(asyncio.run(server.call_tool(name, arguments)))


def _vision_config(**kwargs):
    defaults = dict(provider="anthropic", model="m", api_key="k")
    defaults.update(kwargs)
    return llm.Config(**defaults)


class TestVisionDisabledByDefault:
    def test_disabled_returns_metadata_without_calling_the_llm(self, tmp_path, monkeypatch):
        path = tmp_path / "shot.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

        def must_not_post(*args, **kwargs):
            raise AssertionError("no model call while vision is disabled")

        monkeypatch.setattr(llm, "_post_json", must_not_post)
        monkeypatch.delenv("COMMONTRACE_VISION_ENABLED", raising=False)
        out = vision.describe_image(str(path))
        assert isinstance(out, str) and "image/png" in out

    def test_garbage_bytes_never_raise(self, tmp_path, monkeypatch):
        path = tmp_path / "junk.bin"
        path.write_bytes(bytes(range(256)) * 4)
        monkeypatch.delenv("COMMONTRACE_VISION_ENABLED", raising=False)
        assert isinstance(vision.describe_image(str(path)), str)
        monkeypatch.setenv("COMMONTRACE_VISION_ENABLED", "1")
        monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")
        out = vision.describe_image(str(path), enabled=True)
        assert isinstance(out, str)

    def test_missing_file_returns_none_not_a_crash(self, tmp_path):
        assert vision.describe_image(str(tmp_path / "nope.png")) is None

    def test_explicit_flag_beats_a_truthy_env(self, tmp_path, monkeypatch):
        path = tmp_path / "a.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setenv("COMMONTRACE_VISION_ENABLED", "1")

        def must_not_post(*args, **kwargs):
            raise AssertionError("explicit enabled=False must win over the env")

        monkeypatch.setattr(llm, "_post_json", must_not_post)
        assert isinstance(vision.describe_image(str(path), enabled=False), str)

    def test_non_vision_provider_stays_metadata_only(self, tmp_path, monkeypatch):
        path = tmp_path / "a.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")

        def must_not_post(*args, **kwargs):
            raise AssertionError("bedrock has no image path and must not be called")

        monkeypatch.setattr(llm, "_post_json", must_not_post)
        out = vision.describe_image(
            str(path), enabled=True,
            config=llm.Config(provider="bedrock", model="m", api_key="", region="r"),
        )
        assert isinstance(out, str)


class TestOllamaAlias:
    def test_ollama_defaults_to_localhost_without_a_key(self, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "ollama")
        monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
        monkeypatch.delenv("COMMONTRACE_LLM_BASE_URL", raising=False)
        config = llm.load_config()
        assert config.provider == "openai-compatible"
        assert config.base_url == "http://localhost:11434/v1"

    def test_explicit_base_url_still_wins(self, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "ollama")
        monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
        monkeypatch.setenv("COMMONTRACE_LLM_BASE_URL", "http://gpu-box:11434/v1")
        assert llm.load_config().base_url == "http://gpu-box:11434/v1"

    def test_openai_compatible_still_requires_a_base_url(self, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_LLM_PROVIDER", "openai-compatible")
        monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")
        monkeypatch.delenv("COMMONTRACE_LLM_BASE_URL", raising=False)
        with pytest.raises(llm.LLMUnavailable, match="COMMONTRACE_LLM_BASE_URL"):
            llm.load_config()


class TestImagePayloadShapes:
    def test_anthropic_uses_a_base64_source_block(self, monkeypatch):
        captured = {}

        def fake_post(url, headers, payload):
            captured.update(url=url, headers=headers, payload=payload)
            return {"content": [{"type": "text", "text": "a cat"}],
                    "usage": {"input_tokens": 50, "output_tokens": 9}}

        monkeypatch.setattr(llm, "_post_json", fake_post)
        text, usage = llm.complete_with_image(
            _vision_config(), "what?", b"\xff\xd8fake", "image/jpeg")
        assert text == "a cat" and usage == {"input_tokens": 50, "output_tokens": 9}
        assert captured["url"] == llm._ANTHROPIC_URL
        content = captured["payload"]["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "what?"}
        assert content[1]["type"] == "image"
        assert content[1]["source"]["media_type"] == "image/jpeg"
        assert base64.b64decode(content[1]["source"]["data"]) == b"\xff\xd8fake"

    def test_openai_compatible_uses_a_data_uri(self, monkeypatch):
        captured = {}

        def fake_post(url, headers, payload):
            captured.update(url=url, payload=payload)
            return {"choices": [{"message": {"content": "a dog"}}],
                    "usage": {"prompt_tokens": 60, "completion_tokens": 3}}

        monkeypatch.setattr(llm, "_post_json", fake_post)
        config = _vision_config(provider="openai-compatible", base_url="http://x:11434/v1")
        text, usage = llm.complete_with_image(config, "what?", b"imgbytes", "image/png")
        assert text == "a dog" and usage == {"input_tokens": 60, "output_tokens": 3}
        assert captured["url"] == "http://x:11434/v1/chat/completions"
        part = captured["payload"]["messages"][0]["content"][1]
        assert part["type"] == "image_url"
        assert part["image_url"]["url"] == (
            "data:image/png;base64," + base64.b64encode(b"imgbytes").decode("ascii"))

    def test_ollama_config_takes_the_openai_path(self, monkeypatch):
        seen = {}

        def fake_post(url, headers, payload):
            seen["url"] = url
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

        monkeypatch.setattr(llm, "_post_json", fake_post)
        text, _ = llm.complete_with_image(
            llm.Config(provider="ollama", model="m", api_key=""), "p", b"b", "image/png")
        assert text == "ok" and seen["url"].startswith("http://localhost:11434/v1/")


class TestGraphVizMcpTool:
    def test_tool_is_registered(self):
        assert "graph_viz_html" in mcp_tools.LOCAL_TOOLS

    def test_tool_is_callable_and_writes_the_page(self, tmp_path, monkeypatch):
        pytest.importorskip("mcp", reason="needs the MCP SDK")
        from commontrace import graph_viz, mcp_server

        monkeypatch.setattr(
            graph_viz, "render_html",
            lambda root, as_of=None: "<html><body>GRAPH</body></html>")
        server = mcp_server.build_server(str(tmp_path))
        names = {t.name for t in asyncio.run(server.list_tools())}
        assert "graph_viz_html" in names
        payload = _call(server, "graph_viz_html")
        assert payload["ok"] is True
        assert payload["html_path"].endswith("graph.html")
        assert "GRAPH" in payload["html_head"]
        with io.open(payload["html_path"], encoding="utf-8") as fh:
            assert "GRAPH" in fh.read()

    def test_render_failure_is_an_answer_not_a_crash(self, tmp_path, monkeypatch):
        pytest.importorskip("mcp", reason="needs the MCP SDK")

        from commontrace import graph_viz, mcp_server

        def boom(root, as_of=None):
            raise RuntimeError("no graph today")

        monkeypatch.setattr(graph_viz, "render_html", boom)
        server = mcp_server.build_server(str(tmp_path))
        payload = _call(server, "graph_viz_html")
        assert payload["ok"] is False and "could not render" in payload["error"]

    def test_vision_end_to_end_through_a_mocked_model(self, tmp_path):
        def fake_post(url, headers, payload):
            return {"content": [{"type": "text", "text": "a red door"}],
                    "usage": {"input_tokens": 1, "output_tokens": 2}}

        import unittest.mock as mock

        path = tmp_path / "door.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        with mock.patch.object(llm, "_post_json", fake_post):
            out = vision.describe_image(str(path), enabled=True,
                                        config=_vision_config())
        assert out == "a red door"
