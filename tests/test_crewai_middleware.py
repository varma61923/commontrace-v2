from types import SimpleNamespace

from unimem.client import MemoryClient
from unimem.framework_middleware import Middleware
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord


def test_crewai_callbacks_use_scoped_memory_and_capture_completion(tmp_path):
    owner = MemoryClient(str(tmp_path), agent_id="crew", context={"user": "alice"})
    Journal(str(tmp_path)).put(
        MemoryRecord(
            "alice-directive",
            "directive",
            1,
            {"text": "Always verify the blue release gate."},
            scope={"agent": "crew", "user": "alice"},
        )
    )
    middleware = Middleware(owner, "crew-run")
    inputs = {"task": "release", "unimem_memory": "stale context"}

    prepared = middleware.crewai_before_kickoff(inputs)

    assert "blue release gate" in prepared["unimem_memory"]
    assert "stale context" not in str(prepared)
    assert inputs["unimem_memory"] == "stale context"

    other = Middleware(MemoryClient(str(tmp_path), agent_id="crew", context={"user": "bob"}), "other-run")
    other_inputs = other.crewai_before_kickoff({"task": "release"})
    assert "blue release gate" not in other_inputs["unimem_memory"]

    output = SimpleNamespace(raw="Release completed with the required check.")
    assert middleware.crewai_after_kickoff(output) is output
    captured = Journal(str(tmp_path)).buffer(middleware.run_key, scope=middleware.scope)
    session_end = next(item for item in captured if item["event"] == "SessionEnd")
    assert session_end["payload"]["succeeded"] is True
    assert session_end["payload"]["result"] == output.raw


def test_crewai_callbacks_fail_open(tmp_path, monkeypatch):
    middleware = Middleware(MemoryClient(str(tmp_path), agent_id="crew"), "run")

    def fail_retrieval(*args, **kwargs):
        raise RuntimeError("memory is unavailable")

    monkeypatch.setattr(middleware, "before_run", fail_retrieval)
    inputs = {"task": "continue", "unimem_memory": "stale context"}
    assert middleware.crewai_before_kickoff(inputs) == {"task": "continue"}

    def fail_capture(*args, **kwargs):
        raise RuntimeError("memory is unavailable")

    monkeypatch.setattr(middleware, "after_run", fail_capture)
    output = object()
    assert middleware.crewai_after_kickoff(output) is output


def test_crewai_after_callback_preserves_explicit_failure_outcome(tmp_path):
    middleware = Middleware(MemoryClient(str(tmp_path), agent_id="crew"), "run")
    output = "Crew returned a result that failed the caller's acceptance check."

    assert middleware.crewai_after_kickoff(output, succeeded=False) is output
    captured = Journal(str(tmp_path)).buffer(middleware.run_key, scope=middleware.scope)
    session_end = next(item for item in captured if item["event"] == "SessionEnd")
    assert session_end["payload"]["succeeded"] is False


def test_package_import_does_not_import_crewai():
    import os
    import subprocess
    import sys

    code = (
        "import builtins\n"
        "original = builtins.__import__\n"
        "def guarded(name, *args, **kwargs):\n"
        "    if name == 'crewai' or name.startswith('crewai.'):\n"
        "        raise AssertionError('UniMem imported optional CrewAI')\n"
        "    return original(name, *args, **kwargs)\n"
        "builtins.__import__ = guarded\n"
        "import unimem\n"
        "import unimem.framework_middleware\n"
    )
    env = {**os.environ, "PYTHONPATH": os.getcwd()}
    subprocess.run([sys.executable, "-c", code], check=True, env=env, cwd=os.getcwd())
