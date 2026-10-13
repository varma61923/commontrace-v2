from __future__ import annotations

import asyncio
import base64
import io
import json
import urllib.error

import pytest

from commontrace import llm, mcp_tools, vision
from commontrace.circuit_breaker import CircuitBreaker


@pytest.fixture(autouse=True)
def isolated_provider_state():
    llm._CIRCUITS.clear()
    llm._OVERLOADS.clear()
    yield
    llm._CIRCUITS.clear()
    llm._OVERLOADS.clear()


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

        def unavailable(*_args):
            raise TimeoutError("mock provider unavailable")

        monkeypatch.setattr(llm, "_post_json", unavailable)
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


class TestVisionProviderRecovery:
    def test_provider_retry_after_from_vision_applies_to_text(self, monkeypatch):
        from commontrace.overload import Overloaded, OverloadPolicy

        policy = OverloadPolicy(clock=lambda: 100.0)
        monkeypatch.setattr(llm._OVERLOADS, "get_or_load", lambda _key, _loader: policy)
        monkeypatch.setenv("COMMONTRACE_LLM_CIRCUIT_BREAKER", "1")
        monkeypatch.setenv("COMMONTRACE_LLM_CACHE", "0")
        calls = []

        def limited(*args):
            calls.append(args)
            raise urllib.error.HTTPError("https://example", 429, "limited", {"Retry-After": "20"}, None)

        monkeypatch.setattr(llm, "_post_json", limited)
        config = _vision_config()
        with pytest.raises(urllib.error.HTTPError):
            llm.complete_with_image(config, "p", b"b", "image/png")
        with pytest.raises(Overloaded) as error:
            llm.complete("p", config)
        assert error.value.retry_after == 20
        assert len(calls) == 1

    def test_cached_text_survives_vision_outage_without_caching_images(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_LLM_CACHE", "1")
        monkeypatch.setenv("COMMONTRACE_LLM_CACHE_PATH", str(tmp_path / "llm.db"))
        monkeypatch.setenv("COMMONTRACE_LLM_CIRCUIT_BREAKER", "1")
        config = _vision_config()
        monkeypatch.setattr(llm, "_call_anthropic", lambda *_args: ("cached text", {}))
        assert llm.complete("same prompt", config)[0] == "cached text"

        def unavailable(*_args):
            raise TimeoutError("offline")

        monkeypatch.setattr(llm, "_post_json", unavailable)
        for _ in range(5):
            with pytest.raises(TimeoutError):
                llm.complete_with_image(config, "same prompt", b"b", "image/png")
        text, usage = llm.complete("same prompt", config)
        assert text == "cached text" and usage["_commontrace_cache_hit"] is True
        with pytest.raises(llm.LLMUnavailable, match="retry in"):
            llm.complete_with_image(config, "same prompt", b"b", "image/png")

    def test_vision_outage_protects_text_and_isolates_provider_context(self, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_LLM_CIRCUIT_BREAKER", "1")
        monkeypatch.setenv("COMMONTRACE_LLM_CACHE", "0")
        calls = []

        def unavailable(*args):
            calls.append(args)
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(llm, "_post_json", unavailable)
        config = _vision_config()
        for _ in range(5):
            with pytest.raises(urllib.error.URLError):
                llm.complete_with_image(config, "p", b"b", "image/png")
        with pytest.raises(llm.LLMUnavailable, match="retry in"):
            llm.complete_with_image(config, "p", b"b", "image/png")
        with pytest.raises(llm.LLMUnavailable, match="retry in"):
            llm.complete("p", config)
        assert len(calls) == 5
        for other in (_vision_config(api_key="other"), _vision_config(cache_namespace="other"),
                      _vision_config(base_url="https://other"), _vision_config(model="other")):
            with pytest.raises(urllib.error.URLError):
                llm.complete_with_image(other, "p", b"b", "image/png")
        assert len(calls) == 9
        monkeypatch.setenv("COMMONTRACE_LLM_CIRCUIT_BREAKER", "0")
        with pytest.raises(urllib.error.URLError):
            llm.complete_with_image(config, "p", b"b", "image/png")
        assert len(calls) == 10

    def test_recovery_and_permanent_errors(self, monkeypatch):
        clock = [100.0]
        breaker = CircuitBreaker(threshold=1, recovery_seconds=10, clock=lambda: clock[0])
        monkeypatch.setattr(llm._CIRCUITS, "get_or_load", lambda _key, _loader: breaker)
        monkeypatch.setenv("COMMONTRACE_LLM_CIRCUIT_BREAKER", "1")

        def unavailable(*_args):
            raise TimeoutError("offline")

        monkeypatch.setattr(llm, "_post_json", unavailable)
        config = _vision_config()
        with pytest.raises(TimeoutError):
            llm.complete_with_image(config, "p", b"b", "image/png")
        with pytest.raises(llm.LLMUnavailable, match="retry in"):
            llm.complete_with_image(config, "p", b"b", "image/png")
        clock[0] += 10
        monkeypatch.setattr(llm, "_post_json", lambda *_args: {"content": [{"text": "healthy"}]})
        assert llm.complete_with_image(config, "p", b"b", "image/png")[0] == "healthy"

        def unauthorized(*_args):
            raise urllib.error.HTTPError("https://example", 401, "unauthorized", {}, None)

        monkeypatch.setattr(llm, "_post_json", unauthorized)
        for _ in range(6):
            with pytest.raises(urllib.error.HTTPError):
                llm.complete_with_image(config, "p", b"b", "image/png")

    def test_outage_and_open_circuit_fall_back_to_metadata(self, tmp_path, monkeypatch):
        path = tmp_path / "photo.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setenv("COMMONTRACE_LLM_CIRCUIT_BREAKER", "1")
        calls = []

        def unavailable(*args):
            calls.append(args)
            raise TimeoutError("offline")

        monkeypatch.setattr(llm, "_post_json", unavailable)
        for _ in range(7):
            assert vision.describe_image(str(path), config=_vision_config(), enabled=True) == (
                "image photo.png (image/png, 8 bytes)")
        assert len(calls) == 5

    def test_oversized_images_use_bounded_reads(self, tmp_path, monkeypatch):
        path = tmp_path / "large.png"
        with path.open("wb") as stream:
            stream.write(b"\x89PNG\r\n\x1a\n")
            stream.truncate(vision.MAX_IMAGE_BYTES * 4)
        reads = []
        original_open = open

        class GuardedFile:
            def __enter__(self):
                self.stream = original_open(path, "rb")
                return self

            def __exit__(self, *_args):
                self.stream.close()

            def fileno(self):
                return self.stream.fileno()

            def read(self, count=-1):
                reads.append(count)
                assert 0 <= count <= vision.MAX_IMAGE_BYTES + 1
                return self.stream.read(count)

        monkeypatch.setattr(vision, "open", lambda *_args: GuardedFile(), raising=False)
        monkeypatch.setattr(llm, "complete_with_image", lambda *_args: pytest.fail("oversized image sent"))
        assert vision.describe_image(str(path), config=_vision_config(), enabled=True) == (
            f"image large.png (image/png, {vision.MAX_IMAGE_BYTES * 4} bytes)")
        assert reads == [vision.MAX_IMAGE_BYTES + 1]


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
