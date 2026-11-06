"""Publishing requires successful CI for the actual checkout, never an older or forked run."""

import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
import yaml

from scripts import check_release_ci as gate
from scripts import first_recall_smoke as smoke

SHA = "a" * 40
REPO = "owner/unimem"


def run(**changes):
    return {
        "id": 10,
        "head_sha": SHA,
        "head_repository": {"full_name": REPO},
        "event": "push",
        "path": ".github/workflows/ci.yml@main",
        "status": "completed",
        "conclusion": "success",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"head_sha": "b" * 40},
        {"head_repository": {"full_name": "fork/unimem"}},
        {"head_repository": None},
        {"event": "pull_request"},
        {"path": ".github/workflows/unrelated.yml"},
        {"status": "in_progress", "conclusion": None},
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"id": "invalid"},
    ],
)
def test_ci_evidence_must_be_successful_and_bound_to_this_exact_trusted_commit(changes):
    with pytest.raises(ValueError):
        gate.successful_run({"workflow_runs": [run(**changes)]}, REPO, SHA)


def test_an_older_success_cannot_hide_a_new_failed_or_pending_run():
    for status in ("in_progress", "completed"):
        with pytest.raises(ValueError, match="newest"):
            gate.successful_run(
                {"workflow_runs": [run(id=1), run(id=2, status=status, conclusion="failure")]}, REPO, SHA
            )
    assert gate.successful_run({"workflow_runs": [run(id=1, conclusion="failure"), run(id=2)]}, REPO, SHA) == 2
    assert gate.successful_run({"workflow_runs": [run(event="workflow_dispatch")]}, REPO, SHA) == 10


@pytest.mark.parametrize("payload", [[], {}, {"workflow_runs": None}])
def test_missing_or_malformed_ci_evidence_fails_closed(payload):
    with pytest.raises(ValueError):
        gate.successful_run(payload, REPO, SHA)


def test_ci_api_request_is_bounded_and_does_not_print_credentials(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-private-token")
    monkeypatch.delenv("GITHUB_API_URL", raising=False)

    class Opener:
        def open(self, request, *, timeout):
            assert timeout == 15
            assert "head_sha=" + SHA in request.full_url
            assert request.headers["Authorization"] == "Bearer fixture-private-token"
            return io.BytesIO(json.dumps({"workflow_runs": [run()]}).encode())

    monkeypatch.setattr(gate.urllib.request, "build_opener", lambda *_: Opener())
    assert gate.main(["--sha", SHA]) == 0
    assert "fixture-private-token" not in capsys.readouterr().out

    refused_body = io.BytesIO(b"fixture-private-token")

    def refused(*_):
        raise HTTPError("https://api.github.com", 503, "unavailable", {}, refused_body)

    monkeypatch.setattr(gate.urllib.request, "build_opener", refused)
    assert gate.main(["--sha", SHA]) == 1
    assert refused_body.closed
    assert "fixture-private-token" not in capsys.readouterr().err


def test_ci_redirects_are_refused_before_authentication_can_cross_origins():
    body = io.BytesIO(b"redirect response")
    with pytest.raises(ValueError, match="redirect"):
        gate._NoRedirect().redirect_request(None, body, 302, "redirect", {}, "https://elsewhere.example")
    assert body.closed


def test_both_publish_jobs_require_the_ci_gate_and_the_wheel_journey_is_wired():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
    jobs = yaml.safe_load(path.read_text())["jobs"]
    for name in ("publish-pypi", "publish-npm"):
        assert "verify-ci" in jobs[name]["needs"]
    assert jobs["verify-ci"]["permissions"] == {"contents": "read", "actions": "read"}
    assert any("git rev-parse HEAD" in step.get("run", "") for step in jobs["verify-ci"]["steps"])
    assert any("first_recall_smoke.py" in step.get("run", "") for step in jobs["build"]["steps"])


def test_smoke_uses_one_decreasing_deadline_and_checks_real_recall_content(monkeypatch):
    monkeypatch.setattr(smoke.sys, "argv", ["smoke"])
    monkeypatch.setattr(smoke.shutil, "which", lambda _: "/installed/unimem")
    ticks = iter(range(100))
    monkeypatch.setattr(smoke.time, "perf_counter", lambda: next(ticks))
    timeouts = []

    def execute(args, **kwargs):
        assert args[0] == "/installed/unimem"
        assert kwargs["env"]["UNIMEM_OFFLINE"] == "1"
        timeouts.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"items": [{"text": smoke.STATEMENT}]}))

    monkeypatch.setattr(smoke.subprocess, "run", execute)
    assert smoke.main() == 0
    assert len(timeouts) == 3 and timeouts[0] > timeouts[1] > timeouts[2] > 0


def test_stalled_smoke_subprocess_returns_a_controlled_error(monkeypatch, capsys):
    monkeypatch.setattr(smoke.sys, "argv", ["smoke", "--timeout", "0.01"])
    monkeypatch.setattr(smoke.shutil, "which", lambda _: "/installed/unimem")

    def stalled(args, **kwargs):
        raise smoke.subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(smoke.subprocess, "run", stalled)
    assert smoke.main() == 1
    assert "first-recall smoke failed" in capsys.readouterr().err


@pytest.mark.parametrize("payload", [[], {"items": None}, {"items": [None]}, {"items": [{"text": 1}]}])
def test_malformed_recall_output_refuses_the_release_journey(monkeypatch, capsys, payload):
    monkeypatch.setattr(smoke.sys, "argv", ["smoke"])
    monkeypatch.setattr(smoke.shutil, "which", lambda _: "/installed/unimem")
    monkeypatch.setattr(
        smoke.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload))
    )
    assert smoke.main() == 1
    assert "first-recall smoke failed" in capsys.readouterr().err
