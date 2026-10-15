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
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

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


def serve(root: str, *, profile: str | None = None):
    config = configuration.load(root, profile=profile)
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
                bridged = None
                while True:
                    try:
                        # Lessons approved after onboarding must reach the injector without a re-run of `up`.
                        from commontrace import bootstrap_runtime

                        signature = bootstrap_runtime.lessons_signature(root)
                        if signature != bridged:
                            bootstrap_runtime.bridge_all(root)
                            bridged = signature
                    except Exception:  # noqa: BLE001 - the next pass retries
                        pass
                    try:
                        # New and retired facts reach the knowledge graph without an explicit command.
                        from commontrace import graph_sync

                        graph_sync.run_if_due(root)
                    except Exception:  # noqa: BLE001 - the next pass retries
                        pass
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

            # Create the socket owner-only from the start: a chmod after bind() leaves a window in
            # which a permissive process umask would let another local user connect.
            previous_umask = os.umask(0o177)
            try:
                server.bind(endpoint)
            finally:
                os.umask(previous_umask)
            os.chmod(endpoint, 0o600)
            server.listen(32)
            threading.Thread(target=copy_context().run, args=(prepare,), daemon=True).start()
            slots = threading.BoundedSemaphore(8)

            def handle_client(client):
                try:
                    with client, configuration.apply(config):
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
                                return
                            from commontrace.native_hooks import normalize

                            event, payload = normalize(data.get("format"), data["event"], data["payload"])
                            kit = HookKit(
                                root,
                                data["agent"],
                                scope=data["scope"],
                                embedder=provider if ready.is_set() else "none",
                            )
                            result = kit.handle(
                                event, payload, defer_delivery=True, capture_only=data.get("format") == "windsurf"
                            )
                            client.sendall(json.dumps(result).encode() + b"\n")
                            # Native bridges acknowledge only after writing context to the host.
                            # Legacy Python clients may instead open a separate receipt connection.
                            if result.get("delivery_token"):
                                client.settimeout(0.02)
                                try:
                                    receipt = b""
                                    while b"\n" not in receipt and len(receipt) <= 4096:
                                        chunk = client.recv(4096)
                                        if not chunk:
                                            break
                                        receipt += chunk
                                    if receipt:
                                        ack = json.loads(receipt)
                                        if (
                                            ack.get("operation") == "ack"
                                            and ack.get("delivery_token") == result["delivery_token"]
                                        ):
                                            kit.journal.commit_delivery(result["delivery_token"], scope=kit.scope)
                                except (OSError, ValueError):
                                    pass
                        except Exception:  # noqa: BLE001 - isolate provider failures from host hooks
                            try:
                                client.sendall(b'{"context":"","injected":[]}\n')
                            except OSError:
                                pass
                finally:
                    slots.release()

            try:
                with ThreadPoolExecutor(max_workers=8, thread_name_prefix="commontrace-hook") as workers:
                    while True:
                        client, _ = server.accept()
                        if not slots.acquire(blocking=False):
                            client.close()
                            continue
                        workers.submit(handle_client, client)

            finally:
                os.remove(endpoint)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--profile", choices=("local", "team", "server"))
    args = parser.parse_args(argv)
    if not hasattr(socket, "AF_UNIX"):
        return 0  # Framework/HTTP middleware remains available on platforms without Unix sockets.
    serve(paths.resolve_root(args.dest), profile=args.profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
