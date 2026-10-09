"""Air-gapped mode refuses non-local network use before any connection is attempted."""
import os

import pytest

from commontrace import client, llm, offline
from commontrace.cli import main
from commontrace.connectors import web_crawler
from commontrace.exceptions import CapabilityError


@pytest.fixture
def offline_env(monkeypatch):
    monkeypatch.setenv(offline.ENV, "1")
    for name in offline.MODEL_HUB_SWITCHES:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.mark.parametrize("url,local", [
    ("http://localhost:11434/api/chat", True), ("http://127.0.0.1:8787/v1", True),
    ("http://[::1]:8000/", True), ("http://api.localhost/x", True),
    ("https://api.openai.com/v1/chat", False), ("http://10.0.0.5/", False),
    ("http://127.0.0.1.evil.example/", False), ("http://localhost.evil.example/", False),
])
def test_loopback_classification_needs_no_dns(url, local):
    assert offline.is_loopback(url) is local


def test_check_url_is_a_no_op_when_online(monkeypatch):
    monkeypatch.delenv(offline.ENV, raising=False)
    offline.check_url("https://example.com", "test")


def test_check_url_refuses_remote_hosts_with_a_capability_error(offline_env):
    with pytest.raises(CapabilityError) as caught:
        offline.check_url("https://example.com/x", "the test connector")
    assert "COMMONTRACE_OFFLINE" in caught.value.remediation
    offline.check_url("http://127.0.0.1:9/x", "local runtime")


def test_hosted_llm_calls_are_refused_before_connecting(offline_env, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("no connection may be attempted")
    monkeypatch.setattr("urllib.request.OpenerDirector.open", boom)
    with pytest.raises(llm.LLMUnavailable, match="offline mode"):
        llm._post_json("https://api.anthropic.com/v1/messages", {}, {})


def test_web_crawler_and_remote_client_refuse(offline_env):
    with pytest.raises(CapabilityError):
        web_crawler._get("https://example.com/", "ua", 1.0, 10)
    remote = client.MemoryClient(url="https://memory.example.com", token="t")
    with pytest.raises(CapabilityError):
        remote._request("search", {})


def test_cli_flag_sets_model_hub_cache_only_switches(tmp_path, monkeypatch):
    monkeypatch.delenv(offline.ENV, raising=False)
    for name in offline.MODEL_HUB_SWITCHES:
        monkeypatch.delenv(name, raising=False)
    assert main(["--offline", "init", "--dest", str(tmp_path)]) == 0
    assert offline.status() == {"offline": True, "model_hub_cache_only": True}
    assert all(os.environ[name] == "1" for name in offline.MODEL_HUB_SWITCHES)
