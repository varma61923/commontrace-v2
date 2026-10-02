"""Keep the semantic retriever loaded between `commontrace query` calls."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback

PROTOCOL = 1
DEFAULT_IDLE_SECONDS = 600.0
_SPAWN_WAIT_SECONDS = 3.0
_REPLY_TIMEOUT_SECONDS = 180.0
_BUILD_REPLY_TIMEOUT_SECONDS = 7200.0
_MAX_REQUEST_BYTES = 8 << 20
_MAX_REPLY_BYTES = 64 << 20
_MAX_SOCKET_PATH = 100
_TICK_SECONDS = 5.0
_HERE = os.path.dirname(os.path.abspath(__file__))
_QUERY_SCRIPT = os.path.join("memory", "attention", "query.py")
_BUILD_SCRIPT = os.path.join("memory", "attention", "build_index.py")


_LOCAL = None


def available() -> bool:
    """Whether this platform and environment allow a worker at all."""
    if _LOCAL is not None:
        return False
    if os.name != "posix" or not hasattr(socket, "AF_UNIX"):
        return False
    return os.environ.get("COMMONTRACE_WARM", "").strip().lower() not in {"0", "false", "no", "off"}


def enabled(relative: str) -> bool:
    """Whether a call to the reference script `relative` may use a worker."""
    return os.path.normpath(relative) in (_QUERY_SCRIPT, _BUILD_SCRIPT) and (
        _LOCAL is not None or available())


def worker_script(relative: str, script: str) -> str:
    if os.path.normpath(relative) == _BUILD_SCRIPT:
        return os.path.join(os.path.dirname(script), "query.py")
    return script


def packaged_query_script() -> str:
    return os.path.join(_HERE, "reference", "query.py")


def _idle_seconds() -> float:
    try:
        value = float(os.environ.get("COMMONTRACE_WARM_IDLE", DEFAULT_IDLE_SECONDS))
    except ValueError:
        return DEFAULT_IDLE_SECONDS
    return value if value > 0 else DEFAULT_IDLE_SECONDS


def runtime_dir() -> str | None:
    base = os.environ.get("XDG_RUNTIME_DIR") or ""
    if base and os.path.isdir(base):
        path = os.path.join(base, "commontrace")
    else:
        path = os.path.join(tempfile.gettempdir(), "commontrace-%d" % os.getuid())
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return None
    import stat as _stat

    if not _stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        return None
    return path


def _fingerprint(path: str) -> str:
    st = os.stat(path)
    return "%s:%d:%d" % (os.path.realpath(path), st.st_mtime_ns, st.st_size)


def _package_fingerprint() -> str:
    stamps = []
    for directory, dirnames, filenames in os.walk(_HERE):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".py"):
                full = os.path.join(directory, name)
                st = os.stat(full)
                stamps.append("%s:%d:%d" % (os.path.relpath(full, _HERE), st.st_mtime_ns, st.st_size))
    return hashlib.sha256("\n".join(stamps).encode("utf-8")).hexdigest()


def socket_path(script: str) -> str | None:
    directory = runtime_dir()
    if directory is None:
        return None
    try:
        key = "|".join([
            str(PROTOCOL), os.path.realpath(sys.executable), _fingerprint(script), _package_fingerprint(),
        ])
    except OSError:
        return None
    name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    path = os.path.join(directory, name + ".sock")
    return path if len(path) <= _MAX_SOCKET_PATH else None


def _send(conn: socket.socket, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    conn.sendall(struct.pack(">I", len(data)) + data)


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    chunks = []
    while n:
        chunk = conn.recv(min(n, 1 << 16))
        if not chunk:
            raise ConnectionError("peer closed the connection")
        chunks.append(chunk)
        n -= len(chunk)
    return b"".join(chunks)


def _recv(conn: socket.socket, limit: int) -> dict:
    (length,) = struct.unpack(">I", _recv_exact(conn, 4))
    if length > limit:
        raise ValueError("message of %d bytes exceeds %d" % (length, limit))
    payload = json.loads(_recv_exact(conn, length).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("message is not an object")
    return payload


def _peer_uid(conn: socket.socket) -> int | None:
    opt = getattr(socket, "SO_PEERCRED", None)
    if opt is None:
        return None
    raw = conn.getsockopt(socket.SOL_SOCKET, opt, struct.calcsize("3i"))
    return struct.unpack("3i", raw)[1]


def _reply_timeout(request: dict) -> float:
    return _BUILD_REPLY_TIMEOUT_SECONDS if request.get("op") in ("build", "cli") else _REPLY_TIMEOUT_SECONDS


def _ask(path: str, request: dict, *, connect_deadline: float) -> dict | None:
    return _exchange(path, request, connect_deadline=connect_deadline)[1]


def _exchange(path: str, request: dict, *, connect_deadline: float) -> tuple[bool, dict | None]:
    while True:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.settimeout(1.0)
            conn.connect(path)
        except (FileNotFoundError, ConnectionRefusedError):
            conn.close()
            if time.monotonic() >= connect_deadline:
                return False, None
            time.sleep(0.05)
            continue
        except OSError:
            conn.close()
            return False, None
        break
    try:
        uid = _peer_uid(conn)
        if uid is not None and uid != os.getuid():
            return True, None
        conn.settimeout(_reply_timeout(request))
        _send(conn, request)
        reply = _recv(conn, _MAX_REPLY_BYTES)
    except (OSError, ValueError, struct.error):
        return True, None
    finally:
        conn.close()
    return True, (reply if reply.get("protocol") == PROTOCOL else None)


def _spawn(script: str, path: str, root: str | None, env: dict) -> bool:
    cmd = [sys.executable]
    if sys.version_info >= (3, 11):
        cmd.append("-P")
    cmd.extend([os.path.abspath(__file__), "serve", "--script", script, "--socket", path])
    if root:
        cmd.extend(["--warm-root", root])
    try:
        subprocess.Popen(
            cmd, env=env, cwd=os.path.dirname(path),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError:
        return False
    return True


def _call(script: str, request: dict, root: str | None, env: dict, valid) -> dict | None:
    if _LOCAL is not None:
        return None
    path = socket_path(script)
    if path is None:
        return None
    reached, reply = _exchange(path, request, connect_deadline=0.0)
    if not reached:
        if os.path.exists(path) and not _is_socket(path):
            return None
        if not _spawn(script, path, root, env):
            return None
        reply = _ask(path, request, connect_deadline=time.monotonic() + _SPAWN_WAIT_SECONDS)
    return reply if reply is not None and valid(reply) else None


def run(
    script: str, argv: list[str], root: str, env: dict, *, build: bool = False,
) -> tuple[int, str, str] | None:
    if not os.path.isfile(script):
        return None
    if _LOCAL is not None:
        if os.path.realpath(script) != os.path.realpath(_LOCAL.path):
            return None
        reply = _LOCAL.build(argv, os.path.abspath(root)) if build else _LOCAL.answer(argv, os.path.abspath(root))
        return reply["rc"], reply["stdout"], reply["stderr"]
    request = {"protocol": PROTOCOL, "argv": list(argv), "root": os.path.abspath(root)}
    if build:
        request["op"] = "build"
    reply = _call(script, request, root, env, lambda r: (
        isinstance(r.get("rc"), int) and isinstance(r.get("stdout"), str) and isinstance(r.get("stderr"), str)
    ))
    if reply is None:
        return None
    return reply["rc"], reply["stdout"], reply["stderr"]


CLI_COMMANDS = ("query",)


def run_cli(argv: list[str]) -> tuple[int, str, str] | None:
    if not argv or argv[0] not in CLI_COMMANDS or not available():
        return None
    if os.environ.get("COMMONTRACE_ALLOW_STORE_SCRIPTS", "").strip().lower() in {"1", "true", "yes", "on"}:
        return None
    try:
        cwd = os.getcwd()
    except OSError:
        return None
    env = dict(os.environ)
    request = {"protocol": PROTOCOL, "op": "cli", "argv": list(argv), "cwd": cwd, "env": env}
    spawn_env = dict(env)
    spawn_env["PYTHONUTF8"] = "1"
    spawn_env["PYTHONSAFEPATH"] = "1"
    reply = _call(packaged_query_script(), request, None, spawn_env, lambda r: (
        isinstance(r.get("rc"), int) and isinstance(r.get("stdout"), str) and isinstance(r.get("stderr"), str)
    ))
    if reply is None:
        return None
    return reply["rc"], reply["stdout"], reply["stderr"]


def rerank_scores(mode: str, pairs: list[tuple[str, str]]) -> list[float] | None:
    if not available():
        return None
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONSAFEPATH"] = "1"
    request = {"protocol": PROTOCOL, "op": "rerank", "mode": mode, "pairs": [list(p) for p in pairs]}
    reply = _call(packaged_query_script(), request, None, env, lambda r: (
        isinstance(r.get("scores"), list) and len(r["scores"]) == len(pairs)
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in r["scores"])
    ))
    return None if reply is None else [float(x) for x in reply["scores"]]


def status(script: str) -> tuple[bool, str]:
    """(healthy, one line) for `commontrace doctor`."""
    if not enabled(_QUERY_SCRIPT):
        if os.name != "posix":
            return True, "not available on this platform; each semantic query loads the model"
        return True, "off (COMMONTRACE_WARM=0); each semantic query loads the model"
    if runtime_dir() is None:
        return False, ("off: no private runtime directory (XDG_RUNTIME_DIR or %s must be owned "
                       "by you, mode 0700); each semantic query loads the model" % tempfile.gettempdir())
    path = socket_path(script)
    if path is None:
        return False, "off: the runtime directory path is too long for a Unix socket"
    if _is_socket(path):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(path)
            return True, "running (%s)" % path
        except OSError:
            pass
        finally:
            probe.close()
    return True, "not running; the next semantic query starts it"


def _is_socket(path: str) -> bool:
    import stat as _stat

    try:
        return _stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:
        return False


class _Script:
    def __init__(self, script: str):
        import importlib.util

        self.path = script
        self.fingerprint = _fingerprint(script)
        self.package = _package_fingerprint()
        spec = importlib.util.spec_from_file_location("commontrace_warm_query", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.module = module
        self._models: dict = {}
        self._indexes: dict = {}
        self._builder = None
        self._builder_fingerprint = ""
        load_model, load_index = module.load_model, module.load_index

        def cached_model(model_name=module.DEFAULT_MODEL_NAME):
            if model_name not in self._models:
                model = load_model(model_name)
                if isinstance(model, module.Ranked):
                    return model
                self._models[model_name] = model
            return self._models[model_name]

        def cached_index(index_path):
            try:
                st = os.stat(index_path)
            except OSError:
                return load_index(index_path)
            key = (os.path.abspath(index_path), st.st_ino, st.st_mtime_ns, st.st_size)
            if key not in self._indexes:
                index = load_index(index_path)
                if isinstance(index, module.Ranked):
                    return index
                while len(self._indexes) >= 8:
                    self._indexes.pop(next(iter(self._indexes)))
                self._indexes[key] = index
            return self._indexes[key]

        module.load_model = cached_model
        module.load_index = cached_index

    def changed(self) -> bool:
        try:
            return _fingerprint(self.path) != self.fingerprint or _package_fingerprint() != self.package
        except OSError:
            return True

    def preload(self, root: str) -> None:
        module = self.module
        index = module.load_index(os.path.join(root, "memory", "attention", "index.npz"))
        name = index[0] if not isinstance(index, module.Ranked) else module.DEFAULT_MODEL_NAME
        if name in module.TRUSTED_MODELS:
            module.load_model(name)

    def rerank(self, mode: str, pairs: list[list[str]]) -> dict:
        import importlib

        arm = importlib.import_module("commontrace.rerank_arm")
        if mode not in arm.MODELS:
            raise ValueError("unknown rerank mode")
        with arm._LOCK:
            model = arm._load(mode)
            if not pairs:
                return {"protocol": PROTOCOL, "scores": []}
            scores = model.predict([tuple(p) for p in pairs], batch_size=64, show_progress_bar=False)
        return {"protocol": PROTOCOL, "scores": [float(x) for x in scores]}

    def builder(self):
        path = os.path.join(os.path.dirname(self.path), "build_index.py")
        fingerprint = _fingerprint(path)
        if self._builder is None or fingerprint != self._builder_fingerprint:
            import importlib.util

            spec = importlib.util.spec_from_file_location("commontrace_warm_build_index", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            parse = module._load_frontmatter
            parsed: dict = {}

            def memoised(fm_text):
                if fm_text not in parsed:
                    if len(parsed) >= 50000:
                        parsed.clear()
                    parsed[fm_text] = parse(fm_text)
                return parsed[fm_text]

            module._load_frontmatter = memoised
            real = module.SentenceTransformer
            if real is not None:
                def shared(model_name, *args, **kwargs):
                    if model_name not in self._models:
                        self._models[model_name] = real(model_name, *args, **kwargs)
                    return self._models[model_name]

                module.SentenceTransformer = shared
            self._builder, self._builder_fingerprint = module, fingerprint
        return self._builder

    def build(self, argv: list[str], root: str) -> dict:
        """What `python build_index.py *argv` with COMMONTRACE_ROOT=root would print."""
        module = self.builder()
        module.INDEX_PATH = os.path.join(root, "memory", "attention", "index.npz")
        module.LESSONS_DIR = os.path.join(root, "memory", "lessons")
        return self._main(module, module.__file__, argv)

    def answer(self, argv: list[str], root: str) -> dict:
        """What `python query.py *argv` with COMMONTRACE_ROOT=root would print."""
        module = self.module
        module.INDEX_PATH = os.path.join(root, "memory", "attention", "index.npz")
        module.LESSONS_DIR = os.path.join(root, "memory", "lessons")
        module.TELEMETRY_PATH = os.path.join(root, "memory", "alpha_telemetry.jsonl")
        return self._main(module, self.path, argv)

    def cli(self, argv: list[str], cwd: str, env: dict) -> dict:
        import importlib

        cli = importlib.import_module("commontrace.cli")
        saved_env, saved_cwd = dict(os.environ), os.getcwd()
        os.environ.clear()
        os.environ.update(env)
        try:
            os.chdir(cwd)
            return self._main(None, "commontrace", argv, call=lambda: cli.main(list(argv)))
        finally:
            os.chdir(saved_cwd)
            os.environ.clear()
            os.environ.update(saved_env)

    def _main(self, module, path: str, argv: list[str], call=None) -> dict:
        out, err = io.StringIO(), io.StringIO()
        saved_argv = sys.argv
        sys.argv = [path, *argv]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    rc = call() if call is not None else module.main()
                except SystemExit as exc:
                    rc = _exit_code(exc.code, err)
                except Exception:  # noqa: BLE001 - reported the way an uncaught exception would be
                    traceback.print_exc(file=err)
                    rc = 1
        finally:
            sys.argv = saved_argv
        if rc is None:
            rc = 0
        elif not isinstance(rc, int):
            rc = _exit_code(rc, err)
        return {
            "protocol": PROTOCOL, "rc": rc,
            "stdout": _clean(out.getvalue()), "stderr": _clean(err.getvalue()),
        }


def _exit_code(code, err: io.StringIO) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=err)
    return 1


def _clean(text: str) -> str:
    return text.encode("utf-8", "replace").decode("utf-8")


def _lock(directory: str, name: str):
    import fcntl

    fd = os.open(os.path.join(directory, name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def serve(script: str, path: str, warm_root: str | None, idle: float) -> int:
    directory = os.path.dirname(path)
    if runtime_dir() != directory:
        return 2
    name = os.path.basename(path)[: -len(".sock")]
    lock = _lock(directory, name)
    if lock is None:
        return 0
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o077)
    try:
        server.bind(path)
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
    server.listen(16)
    inode = os.lstat(path).st_ino
    try:
        loaded = _Script(script)
    except BaseException:  # noqa: BLE001 - cannot serve; clients fall back to the subprocess
        with contextlib.suppress(OSError):
            os.unlink(path)
        return 1
    if warm_root:
        with contextlib.suppress(Exception):
            loaded.preload(warm_root)
    _serve_locally(loaded)

    server.settimeout(min(_TICK_SECONDS, idle / 2))
    last = time.monotonic()
    try:
        while True:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                if time.monotonic() - last >= idle or _replaced(path, inode) or loaded.changed():
                    return 0
                continue
            last = time.monotonic()
            with conn:
                _handle(conn, loaded)
            if _replaced(path, inode) or loaded.changed():
                return 0
    finally:
        server.close()
        if not _replaced(path, inode):
            with contextlib.suppress(OSError):
                os.unlink(path)
        os.close(lock)


def _serve_locally(loaded: _Script) -> None:
    global _LOCAL
    _LOCAL = loaded
    with contextlib.suppress(Exception):
        import importlib

        importlib.import_module("commontrace.warm")._LOCAL = loaded


def _replaced(path: str, inode: int) -> bool:
    try:
        return os.lstat(path).st_ino != inode
    except OSError:
        return True


def _handle(conn: socket.socket, loaded: _Script) -> None:
    try:
        conn.settimeout(10.0)
        uid = _peer_uid(conn)
        if uid is not None and uid != os.getuid():
            return
        request = _recv(conn, _MAX_REQUEST_BYTES)
        if request.get("protocol") != PROTOCOL:
            return
        if request.get("op") == "cli":
            argv, cwd, env = request.get("argv"), request.get("cwd"), request.get("env")
            if (
                not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv)
                or argv[0] not in CLI_COMMANDS
                or not isinstance(cwd, str) or not os.path.isabs(cwd)
                or not isinstance(env, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
            ):
                return
            try:
                reply = loaded.cli(argv, cwd, env)
            except Exception as exc:  # noqa: BLE001 - e.g. no package here: the caller runs it itself
                reply = {"protocol": PROTOCOL, "error": "%s: %s" % (type(exc).__name__, exc)}
        elif request.get("op", "query") == "rerank":
            mode, pairs = request.get("mode"), request.get("pairs")
            if not isinstance(mode, str) or not isinstance(pairs, list) or not all(
                isinstance(p, list) and len(p) == 2 and all(isinstance(t, str) for t in p) for p in pairs
            ):
                return
            try:
                reply = loaded.rerank(mode, pairs)
            except Exception as exc:  # noqa: BLE001 - the caller scores in-process instead
                reply = {"protocol": PROTOCOL, "error": "%s: %s" % (type(exc).__name__, exc)}
        else:
            argv, root = request.get("argv"), request.get("root")
            if (
                not isinstance(argv, list) or not all(isinstance(a, str) for a in argv)
                or not isinstance(root, str) or not os.path.isabs(root)
            ):
                return
            if request.get("op", "query") == "build":
                try:
                    reply = loaded.build(argv, root)
                except Exception:  # noqa: BLE001 - no builder here: the caller runs the subprocess
                    return
            else:
                reply = loaded.answer(argv, root)
        conn.settimeout(60.0)
        _send(conn, reply)
    except (OSError, ValueError, struct.error):
        pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="commontrace-warm")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--script", required=True)
    s.add_argument("--socket", required=True)
    s.add_argument("--warm-root", default=None)
    args = parser.parse_args(argv)
    return serve(os.path.abspath(args.script), args.socket, args.warm_root, _idle_seconds())


if __name__ == "__main__":
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]
    sys.exit(main())
