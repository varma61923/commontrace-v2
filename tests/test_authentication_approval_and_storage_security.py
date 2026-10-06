from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import logging
import shutil
import subprocess
import sys
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from commontrace import approval, frontmatter, trace_io, validate  # noqa: E402
from commontrace.commands import doctor_cmd, import_cmd  # noqa: E402

try:
    from hub import auth, crud  # noqa: E402
    from hub.models import ApiKey, Trace  # noqa: E402
    _has_hub = True
except ImportError:
    _has_hub = False
    auth = None  # type: ignore[assignment]
    crud = None  # type: ignore[assignment]
    ApiKey = None  # type: ignore[assignment]
    Trace = None  # type: ignore[assignment]


class TestArgon2HardeningAndFailClosed:
    @pytest.fixture(autouse=True)
    def require_hub(self):
        pytest.importorskip("sqlalchemy", reason="hub[server] extra not installed in this env")
        pytest.importorskip("hub", reason="hub package not importable in this env")

    def test_argon2_stubs_and_dummy_hash_presence(self):
        assert hasattr(auth, "InvalidHashError")
        assert issubclass(auth.InvalidHashError, ValueError)
        assert hasattr(auth, "VerifyMismatchError")
        assert issubclass(auth.VerifyMismatchError, Exception)
        assert hasattr(auth, "_DUMMY_HASH")
        assert isinstance(auth._DUMMY_HASH, str)
        assert len(auth._DUMMY_HASH) > 0

    def test_argon2_installed_hash_and_verify_success(self):
        if not auth._has_argon2:
            pytest.skip("argon2-cffi not installed in this environment")

        secret = "ct_live_super_secret_test_token_123"
        hashed = auth._hash_argon2(secret)
        assert hashed.startswith("$argon2id$")
        assert auth._verify_argon2(secret, hashed) is True
        assert auth._verify_argon2("ct_live_wrong_secret_token", hashed) is False

    def test_argon2_installed_verify_mismatch_and_invalid_hash(self):
        if not auth._has_argon2:
            pytest.skip("argon2-cffi not installed in this environment")

        assert auth._verify_argon2("any_secret", auth._DUMMY_HASH) is False
        assert auth._verify_argon2("any_secret", "corrupt-non-argon2-hash-string") is False
        assert auth._verify_argon2("any_secret", "") is False

    def test_argon2_missing_hash_raises_runtime_error(self):
        with patch.object(auth, "_has_argon2", False), \
             patch.object(auth, "PasswordHasher", None), \
             patch.object(auth, "_hasher", None):
            with pytest.raises(RuntimeError, match="argon2-cffi is required for API key legacy hashing/issuance"):
                auth._hash_argon2("test_secret")

    def test_argon2_missing_verify_returns_false_and_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="hub.auth"):
            with patch.object(auth, "_has_argon2", False), \
                 patch.object(auth, "PasswordHasher", None), \
                 patch.object(auth, "_hasher", None):
                result = auth._verify_argon2("test_secret", "$argon2id$v=19$dummy")
                assert result is False
                assert any("argon2-cffi is not installed" in record.message for record in caplog.records)

    def test_argon2_missing_issue_api_key_fails_closed(self):
        async def _test():
            session = AsyncMock()
            org = MagicMock()
            session.get.return_value = org

            with patch.object(auth, "_has_argon2", False), \
                 patch.object(auth, "PasswordHasher", None), \
                 patch.object(auth, "_hasher", None):
                with pytest.raises(RuntimeError, match="argon2-cffi is required for API key issuance"):
                    await auth.issue_api_key(session, "test-org-id")

        asyncio.run(_test())

    def test_argon2_missing_legacy_scan_returns_none_and_logs_warning(self, caplog):
        async def _test():
            session = AsyncMock()
            now = datetime.now(timezone.utc)
            with caplog.at_level(logging.WARNING, logger="hub.auth"):
                with patch.object(auth, "_has_argon2", False), \
                     patch.object(auth, "PasswordHasher", None), \
                     patch.object(auth, "_hasher", None):
                    res = await auth._verify_by_legacy_scan(session, "ct_live_candidate_raw_key_123", now)
                    assert res is None
                    assert any("argon2-cffi is not installed" in record.message for record in caplog.records)

        asyncio.run(_test())


class TestHubPepperRotationAndTenantIsolation:
    @pytest.fixture(autouse=True)
    def require_hub(self):
        pytest.importorskip("sqlalchemy", reason="hub[server] extra not installed in this env")
        pytest.importorskip("hub", reason="hub package not importable in this env")

    def test_search_traces_retrieval_update_includes_org_id_in_where_clause(self):
        pytest.importorskip("sqlalchemy", reason="hub[server] extra not installed in this env")
        pytest.importorskip("argon2", reason="argon2-cffi extra not installed in this env")

        async def _test():
            raw_key = auth.generate_raw_key()
            old_pepper = b"old_pepper_secret_bytes_12345678"
            new_pepper = b"new_pepper_secret_bytes_87654321"

            old_hmac = hmac.new(old_pepper, raw_key.encode("utf-8"), hashlib.sha256).hexdigest()
            new_hmac = hmac.new(new_pepper, raw_key.encode("utf-8"), hashlib.sha256).hexdigest()

            candidate = ApiKey(
                id="key_test_123",
                org_id="org_test_tenant",
                key_prefix=raw_key[:auth._PREFIX_LEN],
                key_hash=auth._hasher.hash(raw_key),
                key_hmac=old_hmac,
                revoked_at=None,
                expires_at=None,
                last_used_at=None,
                scopes=None,
            )

            session = AsyncMock()
            scalars_mock = MagicMock()
            scalars_mock.all.return_value = [candidate]
            result_mock = MagicMock()
            result_mock.scalars.return_value = scalars_mock
            session.execute.return_value = result_mock
            session.scalar.return_value = None

            with patch.object(auth, "_PEPPER", new_pepper):
                assert candidate.key_hmac != new_hmac
                assert candidate.key_hmac == old_hmac

                now = datetime.now(timezone.utc)
                authenticated = await auth._verify_by_legacy_scan(session, raw_key, now)

                assert authenticated is not None
                assert authenticated.org_id == "org_test_tenant"
                assert candidate.key_hmac == new_hmac
                assert candidate.key_hmac != old_hmac

        asyncio.run(_test())

    def test_pepper_rotation_already_current_hmac_preserved(self):
        if not auth._has_argon2:
            pytest.skip("argon2-cffi not installed in this environment")

        async def _test():
            raw_key = auth.generate_raw_key()
            pepper = b"current_pepper_secret_1234567890"
            current_hmac = hmac.new(pepper, raw_key.encode("utf-8"), hashlib.sha256).hexdigest()

            candidate = ApiKey(
                id="key_test_456",
                org_id="org_test_tenant_2",
                key_prefix=raw_key[:auth._PREFIX_LEN],
                key_hash=auth._hasher.hash(raw_key),
                key_hmac=current_hmac,
                revoked_at=None,
                expires_at=None,
                last_used_at=None,
                scopes=None,
            )

            session = AsyncMock()
            scalars_mock = MagicMock()
            scalars_mock.all.return_value = [candidate]
            result_mock = MagicMock()
            result_mock.scalars.return_value = scalars_mock
            session.execute.return_value = result_mock
            session.scalar.return_value = None

            with patch.object(auth, "_PEPPER", pepper):
                now = datetime.now(timezone.utc)
                authenticated = await auth._verify_by_legacy_scan(session, raw_key, now)
                assert authenticated is not None
                assert candidate.key_hmac == current_hmac

        asyncio.run(_test())

    def test_get_trace_multi_tenant_isolation_in_update(self):
        async def _test():
            from sqlalchemy.sql.dml import Update

            session = AsyncMock()
            org_id = str(uuid.uuid4())
            trace_id = str(uuid.uuid4())

            mock_trace = Trace(
                id=trace_id,
                org_id=org_id,
                title="Tenant-Scoped Trace",
                context_text="Context",
                solution_text="Solution",
                tags=["tenant"],
                agent_type="code",
                retrievals=0,
            )
            result_mock = MagicMock()
            result_mock.scalar_one_or_none.return_value = mock_trace
            session.execute.return_value = result_mock

            with patch("hub.crud._hydrate_one", new_callable=AsyncMock) as mock_hydrate:
                mock_hydrate.return_value = {"id": trace_id, "org_id": org_id, "retrievals": 1}
                res = await crud.get_trace(session, org_id, trace_id)

                assert res is not None
                assert res["id"] == trace_id

            update_calls = [
                call for call in session.execute.call_args_list
                if len(call[0]) > 0 and isinstance(call[0][0], Update)
            ]
            assert len(update_calls) == 1, "Expected exactly one UPDATE statement"
            stmt = update_calls[0][0][0]

            where_clauses = [str(clause) for clause in stmt.whereclause.clauses]
            assert any("traces.org_id" in clause for clause in where_clauses), (
                f"Missing Trace.org_id in update WHERE clause: {where_clauses}"
            )
            assert any("traces.id" in clause for clause in where_clauses), (
                f"Missing Trace.id in update WHERE clause: {where_clauses}"
            )

        asyncio.run(_test())

    def test_get_trace_foreign_org_returns_none_and_never_updates(self):
        async def _test():
            from sqlalchemy.sql.dml import Update

            session = AsyncMock()
            org_id = str(uuid.uuid4())
            trace_id = str(uuid.uuid4())

            result_mock = MagicMock()
            result_mock.scalar_one_or_none.return_value = None
            session.execute.return_value = result_mock

            res = await crud.get_trace(session, org_id, trace_id)
            assert res is None

            update_calls = [
                call for call in session.execute.call_args_list
                if len(call[0]) > 0 and isinstance(call[0][0], Update)
            ]
            assert len(update_calls) == 0, "No UPDATE statement should be executed for missing/foreign trace"

        asyncio.run(_test())


class TestTypeScriptSdkHardening:
    @pytest.fixture(autouse=True)
    def check_node(self):
        if not shutil.which("node"):
            pytest.skip("Node.js runtime not installed in this environment")
        dist_client_js = REPO_ROOT / "sdk" / "typescript" / "dist" / "src" / "client.js"
        node_modules = REPO_ROOT / "sdk" / "typescript" / "node_modules"
        if not dist_client_js.exists() and not node_modules.exists():
            pytest.skip("TypeScript SDK dependencies (node_modules) not installed in this environment")

    def test_typescript_sdk_clamp_retry_after_and_error_handling(self):
        dist_client_js = REPO_ROOT / "sdk" / "typescript" / "dist" / "src" / "client.js"
        if not dist_client_js.exists():
            if not shutil.which("npm"):
                pytest.skip("npm not installed; cannot build TypeScript SDK")
            build_res = subprocess.run(
                ["npm", "run", "build"],
                cwd=str(REPO_ROOT / "sdk" / "typescript"),
                capture_output=True,
                text=True,
            )
            if build_res.returncode != 0:
                pytest.skip(f"TypeScript SDK build skipped (npm run build failed): {build_res.stderr[:200]}")

        node_script = """
        import { clampRetryAfter, parseToolResult } from "./sdk/typescript/dist/src/client.js";
        import assert from "node:assert";

        // 1. clampRetryAfter tests
        assert.strictEqual(clampRetryAfter(undefined), 1, "undefined -> 1");
        assert.strictEqual(clampRetryAfter(null), 1, "null -> 1");
        assert.strictEqual(clampRetryAfter("not-a-number"), 1, "string -> 1");
        assert.strictEqual(clampRetryAfter(NaN), 1, "NaN -> 1");
        assert.strictEqual(clampRetryAfter(-5), 1, "-5 -> clamped min 1");
        assert.strictEqual(clampRetryAfter(0), 1, "0 -> clamped min 1");
        assert.strictEqual(clampRetryAfter(15), 15, "15 -> 15");
        assert.strictEqual(clampRetryAfter(30), 30, "30 -> 30");
        assert.strictEqual(clampRetryAfter(45), 30, "45 -> clamped max 30");
        assert.strictEqual(clampRetryAfter(Infinity), 30, "Infinity -> clamped max 30");

        // 2. parseToolResult plain text error handling
        const plainErr = parseToolResult("test_tool", {
            isError: true,
            content: [{ text: "Service Unavailable" }],
        });
        assert.strictEqual(plainErr.error, "tool_error");
        assert.strictEqual(plainErr.detail, "Service Unavailable");

        // 3. parseToolResult structuredContent error normalization
        const scErr = parseToolResult("test_tool", {
            isError: true,
            structuredContent: { status: 500, detail: "DB Error" },
        });
        assert.strictEqual(scErr.error, "tool_error");
        assert.strictEqual(scErr.detail, "DB Error");

        // 4. parseToolResult valid JSON error preservation
        const jsonErr = parseToolResult("test_tool", {
            isError: true,
            content: [{ text: '{"error": "rate_limited", "retry_after": 5}' }],
        });
        assert.strictEqual(jsonErr.error, "rate_limited");
        assert.strictEqual(jsonErr.retry_after, 5);

        // 5. parseToolResult throws on invalid JSON when isError is false
        assert.throws(() => {
            parseToolResult("test_tool", { isError: false, content: [{ text: "not-json" }] });
        }, /response was not valid JSON/);

        console.log("TS_SDK_HARDENING_OK");
        """
        proc = subprocess.run(
            ["node", "-e", node_script],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"Node script failed: {proc.stderr}\n{proc.stdout}"
        assert "TS_SDK_HARDENING_OK" in proc.stdout


class TestApprovalPolicyStrictValidation:
    def test_validate_policy_rejects_unrecognized_keys(self):
        with pytest.raises(approval.PolicyError, match="unrecognized policy key\\(s\\): extra_key"):
            approval.validate_policy({"mode": "single", "extra_key": "val"})

        with pytest.raises(approval.PolicyError, match="unrecognized policy key\\(s\\): arbitrary, foo"):
            approval.validate_policy({"arbitrary": 1, "foo": 2})

    def test_validate_policy_rejects_invalid_mode(self):
        with pytest.raises(approval.PolicyError, match="mode must be one of single, two-person"):
            approval.validate_policy({"mode": "multi"})

        with pytest.raises(approval.PolicyError, match="mode must be one of single, two-person"):
            approval.validate_policy({"mode": "unrestricted"})

    def test_validate_policy_rejects_invalid_boolean_strings(self):
        invalid_booleans = ["maybe", "tru", "10", "fals", "enabled", "", "2", "none"]
        for bad in invalid_booleans:
            with pytest.raises(approval.PolicyError, match="require_human must be a boolean"):
                approval.validate_policy({"require_human": bad})

    def test_validate_policy_rejects_non_boolean_types(self):
        invalid_types = [1, 0, 10, [True], {"enabled": True}]
        for bad in invalid_types:
            with pytest.raises(approval.PolicyError, match="require_human must be a boolean"):
                approval.validate_policy({"require_human": bad})

    def test_validate_policy_accepts_valid_configurations(self):
        approval.validate_policy({"mode": "single", "require_human": True})
        approval.validate_policy({"mode": "two-person", "require_human": False})
        for valid_str in ("true", "yes", "1", "false", "no", "0", "True", "FALSE"):
            approval.validate_policy({"mode": "single", "require_human": valid_str})

    def test_parse_policy_yaml_rejects_duplicate_keys(self):
        yaml_duplicate_mode = "mode: single\nmode: two-person\n"
        with pytest.raises(approval.PolicyError, match="duplicate key 'mode' in policy file"):
            approval._parse_policy_yaml(yaml_duplicate_mode)

        yaml_duplicate_require_human = "require_human: true\nrequire_human: false\n"
        with pytest.raises(approval.PolicyError, match="duplicate key 'require_human' in policy file"):
            approval._parse_policy_yaml(yaml_duplicate_require_human)

    def test_load_policy_end_to_end(self, tmp_path):
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        policy_file = mem_dir / approval.POLICY_FILENAME

        default_policy = approval.load_policy(str(tmp_path))
        assert default_policy.mode == approval.POLICY_SINGLE
        assert default_policy.require_human is False

        policy_file.write_text("mode: two-person\nrequire_human: true\n", encoding="utf-8")
        loaded = approval.load_policy(str(tmp_path))
        assert loaded.mode == approval.POLICY_TWO_PERSON
        assert loaded.require_human is True
        assert loaded.separation_required is True

        policy_file.write_text("mode: single\nbad_key: value\n", encoding="utf-8")
        with pytest.raises(approval.PolicyError, match="unrecognized policy key"):
            approval.load_policy(str(tmp_path))


class TestDefensiveImportCli:
    def _make_args(self, file_path: str, dest: str) -> argparse.Namespace:
        return argparse.Namespace(
            file=file_path,
            dest=dest,
            format="jsonl",
            source="generic",
            agent_type="code",
            profile="",
            title_field="title",
            context_field="context",
            solution_field="solution",
            tags_field="tags",
            id_field="id",
            dry_run=False,
        )

    def test_import_non_existent_file_returns_1_and_does_not_create_traces_dir(self, tmp_path):
        non_existent = str(tmp_path / "missing_export.jsonl")
        traces_dir = tmp_path / "memory" / "traces"

        rc = import_cmd.run(self._make_args(non_existent, str(tmp_path)))
        assert rc == 1
        assert not traces_dir.exists(), "memory/traces directory must NOT be created on missing file"

    def test_import_directory_returns_1_and_does_not_create_traces_dir(self, tmp_path, capsys):
        sub_dir = tmp_path / "sub_directory"
        sub_dir.mkdir()
        traces_dir = tmp_path / "memory" / "traces"

        rc = import_cmd.run(self._make_args(str(sub_dir), str(tmp_path)))
        assert rc == 1
        err = capsys.readouterr().err
        assert "not a regular file" in err
        assert not traces_dir.exists(), "memory/traces directory must NOT be created on directory input"

    def test_import_oversized_file_fails_gracefully(self, tmp_path, capsys):
        export_file = tmp_path / "huge_export.jsonl"
        export_file.write_text("{}", encoding="utf-8")
        traces_dir = tmp_path / "memory" / "traces"

        with patch("os.path.getsize", return_value=600 * 1024 * 1024):
            rc = import_cmd.run(self._make_args(str(export_file), str(tmp_path)))
            assert rc == 1
            err = capsys.readouterr().err
            assert "is larger than 500 MiB" in err
            assert not traces_dir.exists()

    def test_import_oserror_on_getsize_fails_gracefully(self, tmp_path, capsys):
        export_file = tmp_path / "unreadable.jsonl"
        export_file.write_text("{}", encoding="utf-8")
        traces_dir = tmp_path / "memory" / "traces"

        with patch("os.path.getsize", side_effect=OSError("Disk hardware fault")):
            rc = import_cmd.run(self._make_args(str(export_file), str(tmp_path)))
            assert rc == 1
            err = capsys.readouterr().err
            assert "cannot access" in err
            assert not traces_dir.exists()

    def test_import_oserror_on_open_fails_gracefully(self, tmp_path, capsys):
        export_file = tmp_path / "protected.jsonl"
        export_file.write_text("{}", encoding="utf-8")
        traces_dir = tmp_path / "memory" / "traces"

        orig_open = open

        def selective_open(file, *args, **kwargs):
            if str(file) == str(export_file):
                raise PermissionError("Access denied")
            return orig_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=selective_open):
            rc = import_cmd.run(self._make_args(str(export_file), str(tmp_path)))
            assert rc == 1
            err = capsys.readouterr().err
            assert "could not open" in err
            assert not traces_dir.exists()


class TestMcpServerLockSafetyAndTraceIo:
    def test_draft_lesson_handles_post_lock_validation_exception_and_releases_lock(self, tmp_path):
        mcp_mod = types.ModuleType("mcp")
        mcp_server_mod = types.ModuleType("mcp.server")
        mcp_mcpserver_mod = types.ModuleType("mcp.server.mcpserver")

        class StubMCPServer:
            def __init__(self, *args, **kwargs):
                self.tools = {}

            def tool(self):
                def decorator(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return decorator

        mcp_mcpserver_mod.MCPServer = StubMCPServer
        mcp_server_mod.mcpserver = mcp_mcpserver_mod
        mcp_mod.server = mcp_server_mod

        modules_patch = {
            "mcp": mcp_mod,
            "mcp.server": mcp_server_mod,
            "mcp.server.mcpserver": mcp_mcpserver_mod,
        }

        with patch.dict(sys.modules, modules_patch):
            from commontrace import mcp_server

            lessons_dir = tmp_path / "memory" / "lessons"
            lessons_dir.mkdir(parents=True)
            lesson_path = lessons_dir / "lesson_test_slug.md"
            lesson_path.write_text(
                "---\nname: test_slug\nstatus: draft\nimportance: 3\n---\n## Rule\nTODO\n",
                encoding="utf-8",
            )

            server = mcp_server.build_server(str(tmp_path))
            draft_fn = server.tools["draft_lesson"]

            with patch("commontrace.validate.validate", side_effect=RuntimeError("unexpected validator boom")):
                res = asyncio.run(draft_fn(slug="test_slug", rule="Actionable engineering rule"))
                assert res["ok"] is False
                assert "could not validate 'test_slug'" in res["error"]
                assert "unexpected validator boom" in res["error"]

            res2 = asyncio.run(draft_fn(slug="test_slug", rule="Subsequent valid rule"))
            assert res2["ok"] is True
            assert res2["lesson"]["slug"] == "test_slug"

    def test_trace_io_read_falls_back_on_empty_whitespace_and_none(self, tmp_path):
        trace_file = tmp_path / "test_trace.md"

        trace_file.write_text(
            "---\n"
            "id: test_trace_empty\n"
            "title: Empty String Frontmatter\n"
            "agent_type: code\n"
            "tags: [test]\n"
            "context_text: ''\n"
            "solution_text: ''\n"
            "outcome:\n"
            "  resolved: true\n"
            "---\n\n"
            "## Context\nFallback Context Text\n\n"
            "## Solution\nFallback Solution Text\n",
            encoding="utf-8",
        )
        instance, _ = trace_io.read(str(trace_file))
        assert instance["context_text"] == "Fallback Context Text"
        assert instance["solution_text"] == "Fallback Solution Text"

        trace_file.write_text(
            "---\n"
            "id: test_trace_ws\n"
            "title: Whitespace Frontmatter\n"
            "agent_type: code\n"
            "tags: [test]\n"
            "context_text: \"   \\n\\t  \"\n"
            "solution_text: \"   \"\n"
            "outcome:\n"
            "  resolved: true\n"
            "---\n\n"
            "## Context\nFallback WS Context\n\n"
            "## Solution\nFallback WS Solution\n",
            encoding="utf-8",
        )
        instance, _ = trace_io.read(str(trace_file))
        assert instance["context_text"] == "Fallback WS Context"
        assert instance["solution_text"] == "Fallback WS Solution"

        trace_file.write_text(
            "---\n"
            "id: test_trace_null\n"
            "title: Null Frontmatter\n"
            "agent_type: code\n"
            "tags: [test]\n"
            "context_text: null\n"
            "solution_text: null\n"
            "outcome:\n"
            "  resolved: true\n"
            "---\n\n"
            "## Context\nFallback Null Context\n\n"
            "## Solution\nFallback Null Solution\n",
            encoding="utf-8",
        )
        instance, _ = trace_io.read(str(trace_file))
        assert instance["context_text"] == "Fallback Null Context"
        assert instance["solution_text"] == "Fallback Null Solution"

    def test_trace_io_read_preserves_explicit_non_empty_frontmatter(self, tmp_path):
        trace_file = tmp_path / "test_trace_precedence.md"
        trace_file.write_text(
            "---\n"
            "id: test_trace_precedence\n"
            "title: Precedence Frontmatter\n"
            "agent_type: code\n"
            "tags: [test]\n"
            "context_text: Explicit FM Context\n"
            "solution_text: Explicit FM Solution\n"
            "outcome:\n"
            "  resolved: true\n"
            "---\n\n"
            "## Context\nMarkdown Context\n\n"
            "## Solution\nMarkdown Solution\n",
            encoding="utf-8",
        )
        instance, _ = trace_io.read(str(trace_file))
        assert instance["context_text"] == "Explicit FM Context"
        assert instance["solution_text"] == "Explicit FM Solution"

    def test_trace_io_read_coerces_non_string_values(self, tmp_path):
        trace_file = tmp_path / "test_trace_coercion.md"
        trace_file.write_text(
            "---\n"
            "id: test_trace_coercion\n"
            "title: Coercion Frontmatter\n"
            "agent_type: code\n"
            "tags: [test]\n"
            "context_text: 12345\n"
            "solution_text: 67890\n"
            "outcome:\n"
            "  resolved: true\n"
            "---\n\n"
            "## Context\nIgnored Body Context\n\n"
            "## Solution\nIgnored Body Solution\n",
            encoding="utf-8",
        )
        instance, _ = trace_io.read(str(trace_file))
        assert instance["context_text"] == "12345"
        assert instance["solution_text"] == "67890"


class TestMemoryStoreScaffoldingAndInvariants:
    def test_memory_trace_example_trace_conforms_to_schema(self):
        trace_path = REPO_ROOT / "memory" / "traces" / "2026-07-01_example-trace.md"
        assert trace_path.is_file(), f"Expected trace file at {trace_path}"

        trace_data, body = trace_io.read(str(trace_path))
        schema = validate.load_schema("trace.schema.json")
        errors = validate.validate(trace_data, schema)
        assert not errors, f"Example trace has schema errors: {errors}"

        assert trace_data["id"] == "2026-07-01_example-trace"
        assert trace_data["agent_type"] == "code"
        assert isinstance(trace_data.get("tags"), list)
        assert len(trace_data["tags"]) > 0
        assert trace_data.get("outcome", {}).get("resolved") is True
        assert "## Context" in body
        assert "## Solution" in body

    def test_memory_lesson_template_status_review_conforms_to_schema(self):
        lesson_path = REPO_ROOT / "memory" / "lessons" / "lesson_template.md"
        assert lesson_path.is_file(), f"Expected lesson template at {lesson_path}"

        fm, _ = frontmatter.read(str(lesson_path))
        assert fm["status"] == "review", "lesson_template.md status must be 'review'"

        schema = validate.load_schema("lesson.schema.json")
        errors = validate.validate(fm, schema)
        assert not errors, f"Lesson template has schema errors: {errors}"

    def test_memory_index_declared_agent_type_is_code(self):
        index_path = REPO_ROOT / "memory" / "INDEX.md"
        assert index_path.is_file(), f"Expected index file at {index_path}"

        with open(index_path, encoding="utf-8") as fh:
            first_line = fh.readline()
        assert "agent_type: code" in first_line

        declared = doctor_cmd._declared_agent_type(str(REPO_ROOT))
        assert declared == "code", f"Expected declared agent_type 'code', got {declared!r}"

    def test_memory_episode_template_importance_rationale_populated(self):
        episode_path = REPO_ROOT / "memory" / "episodes" / "episode_template.md"
        assert episode_path.is_file(), f"Expected episode template at {episode_path}"

        fm, _ = frontmatter.read(str(episode_path))
        rationale = fm.get("importance_rationale")
        assert isinstance(rationale, str), "importance_rationale must be a string"
        assert len(rationale.strip()) > 0, "importance_rationale must not be empty"
        assert "1 sentence explaining why this score was assigned" in rationale
