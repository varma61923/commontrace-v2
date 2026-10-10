"""Private local hook broker: keep embedding models warm across native hooks.

The Unix socket is owner-only. HTTP/framework integrations use the authenticated
memory turn API. No network listener, credential or transcript is logged here.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading

from commontrace import _jsonl, configuration, jobs, kg_similarity, paths
from commontrace.hook_kit import HookKit
from commontrace.memory_journal import Journal


def socket_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "hooks.sock")


def request(root: str, agent: str, scope: dict, event: str, payload: dict) -> dict | None:
    if not hasattr(socket, "AF_UNIX") or not os.path.exists(socket_path(root)):
        return None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.04)
        try:
            client.connect(socket_path(root))
            client.sendall(
                json.dumps({"agent": agent, "scope": scope, "event": event, "payload": payload}).encode() + b"\n"
            )
            response = b""
            while b"\n" not in response and len(response) <= 100000:
                chunk = client.recv(64000)
                if not chunk:
                    break
                response += chunk
            return json.loads(response)
        except (OSError, ValueError):
            return {"context": "", "injected": [], "timed_out": True}


def acknowledge(root: str, agent: str, scope: dict, token: str):
    if not token:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.02)
        try:
            client.connect(socket_path(root))
            client.sendall(
                json.dumps({"operation": "ack", "agent": agent, "scope": scope, "delivery_token": token}).encode()
                + b"\n"
            )
        except OSError:
            pass  # Missing receipts never become guessed exposure measurements.


def serve(root: str):
    config = configuration.load(root)
    endpoint = socket_path(root)
    paths.enforce_boundary(root, endpoint)
    Journal(root)
    with _jsonl.locked(endpoint + ".owner"):
        if os.path.exists(endpoint):
            os.remove(endpoint)  # lock ownership proves no other broker owns this endpoint
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server, configuration.apply(config):
            # This dedicated CPU broker favors single-query latency over large-batch throughput.
            os.environ.setdefault("OMP_NUM_THREADS", "1")
            os.environ.setdefault("MKL_NUM_THREADS", "1")
            from commontrace.measure import HarmWatch

            Journal(root)
            HarmWatch(root).current()
            provider = kg_similarity.embedder(config["options"].get("embedding"))
            ready = threading.Event()

            def prepare():
                try:
                    if provider is not None:
                        kg_similarity.embed(provider, ["CommonTrace memory warmup"], query=True)
                    ready.set()
                except (ImportError, ValueError, RuntimeError, OSError):
                    ready.clear()
                while True:
                    try:
                        from commontrace import fact_vector_projection

                        fact_vector_projection.drain(root, model=config["options"].get("embedding", "bge-small"))
                    except Exception:  # noqa: BLE001 - durable queue retains failed jobs
                        pass
                    try:
                        jobs.run_pending(root, limit=1, kinds=["learn-session"])
                        from commontrace.session_learning import reconcile

                        reconcile(root)
                        Journal(root).reap_due()
                    except Exception:  # noqa: BLE001 - worker supervision preserves retries
                        pass
                    threading.Event().wait(1)

            server.bind(endpoint)
            os.chmod(endpoint, 0o600)
            server.listen(32)
            threading.Thread(target=prepare, daemon=True).start()
            try:
                while True:
                    client, _ = server.accept()
                    with client:
                        client.settimeout(0.1)
                        try:
                            incoming = b""
                            while b"\n" not in incoming and len(incoming) <= 100000:
                                chunk = client.recv(64000)
                                if not chunk:
                                    break
                                incoming += chunk
                            data = json.loads(incoming)
                            if data.get("operation") == "ack":
                                Journal(root).commit_delivery(
                                    data["delivery_token"], scope={**data["scope"], "agent": data["agent"]}
                                )
                                continue
                            from commontrace.native_hooks import normalize

                            event, payload = normalize(data.get("format"), data["event"], data["payload"])
                            kit = HookKit(
                                root,
                                data["agent"],
                                scope=data["scope"],
                                embedder=provider if ready.is_set() else "none",
                            )
                            result = kit.handle(event, payload, defer_delivery=True)
                            client.sendall(json.dumps(result).encode() + b"\n")
                        except Exception:  # noqa: BLE001 - isolate provider failures from host hooks
                            try:
                                client.sendall(b'{"context":"","injected":[]}\n')
                            except OSError:
                                pass
            finally:
                os.remove(endpoint)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", required=True)
    args = parser.parse_args(argv)
    if not hasattr(socket, "AF_UNIX"):
        return 0  # Framework/HTTP middleware remains available on platforms without Unix sockets.
    serve(paths.resolve_root(args.dest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
