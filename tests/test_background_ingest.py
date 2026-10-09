"""Background ingestion: queue now, process durably, and drain cleanly on shutdown."""
import json
import os
import signal
import subprocess
import sys
import time

from commontrace import jobs
from commontrace.cli import main


def test_background_ingest_queues_a_job_that_the_worker_completes(tmp_path, capsys):
    root, docs = str(tmp_path / "store"), tmp_path / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("# Deploys\n\nThe staging deploy needs the VPN enabled first.\n")
    assert main(["init", "--dest", root]) == 0
    capsys.readouterr()
    assert main(["ingest", str(docs), "--type", "docs", "--background", "--json", "--dest", root]) == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued["kind"] == "ingest" and queued["status"] == "queued"
    # Re-queueing the same source while it waits is free (one pending job per source).
    assert main(["ingest", str(docs), "--type", "docs", "--background", "--json", "--dest", root]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == queued["id"]
    assert main(["jobs", "run", "--dest", root]) == 0
    done = jobs.get(root, queued["id"])
    assert done.status == "done" and done.result["files"] == 1


def test_background_refuses_preview_and_non_document_types(tmp_path, capsys):
    root = str(tmp_path)
    (tmp_path / "a.md").write_text("x")
    assert main(["ingest", str(tmp_path / "a.md"), "--type", "docs", "--background", "--preview",
                 "--dest", root]) == 2
    assert main(["ingest", str(tmp_path / "a.md"), "--type", "markdown", "--background", "--dest", root]) == 2
    assert main(["ingest", str(tmp_path / "missing"), "--type", "docs", "--background", "--dest", root]) == 2


def test_watching_worker_drains_on_sigterm(tmp_path):
    env = {**os.environ, "PYTHONPATH": os.getcwd()}
    proc = subprocess.Popen([sys.executable, "-m", "commontrace.cli", "jobs", "run", "--watch", "--interval", "0.5",
                             "--dest", str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    time.sleep(3)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err.decode()
    assert b"0 done, 0 failed" in out
