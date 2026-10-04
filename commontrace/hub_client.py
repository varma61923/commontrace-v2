from __future__ import annotations

import asyncio
import contextlib
import glob
import hashlib
import json
import os
import re
import socket
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, TypeVar

from commontrace import frontmatter, paths, templates, trace_io
from commontrace.fingerprints import push_fingerprint as _push_fingerprint
from commontrace.fingerprints import trace_push_fingerprint as _trace_push_fingerprint

_T = TypeVar("_T")

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 0.5
RETRY_MAX_DELAY_SECONDS = 30.0
RATE_LIMIT_MAX_ATTEMPTS = 10
_PACE_MIN_INTERVAL_SECONDS = 0.05
_PACE_MAX_INTERVAL_SECONDS = 2.0
_PACE_DECAY = 0.9

_STATUS_IN_TEXT_RE = re.compile(
    r"(?:\bHTTP[/ ]?(?:\d\.\d )?|\bstatus(?:[ _]?code)?[ =:]+|\bcode[ =:]+)(4\d{2}|5\d{2})\b",
    re.IGNORECASE,
)

_SECTION_NAMES = r"Rule|Why|How to apply|Counter-examples"
_SECTION_RE = re.compile(
    rf"^##\s*({_SECTION_NAMES})\s*\n(.*?)(?=\n##\s*(?:{_SECTION_NAMES})\s*\n|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)


class HubClientUnavailable(RuntimeError):
    """Raised when the optional `mcp` client dependency isn't installed."""


class HubConnectionError(RuntimeError):
    """Raised when the Hub can't be reached or rejects the request."""


class HubConfigurationError(HubConnectionError):
    ...


class _ToolRateLimited(Exception):
    def __init__(self, detail: str, retry_after: float | None = None):
        super().__init__(detail)
        self.retry_after = retry_after


class HubToolError(HubConnectionError):
    ...


class HubAuthError(HubConnectionError):
    """The Hub rejected the credential (401/403)."""


class HubRateLimited(HubConnectionError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


_PUSH_CONCURRENCY = 8


@dataclass
class PushResult:
    slug: str
    hub_trace_id: str | None
    quarantined: bool = False
    error: str | None = None
    skipped: bool = False


@dataclass
class PullResult:
    written_paths: list[str] = field(default_factory=list)
    n_found: int = 0
    ignored_terms: list[str] = field(default_factory=list)


def _routing_fields(fm: dict) -> dict:
    out: dict = {}
    raw = fm.get("scopes")
    scopes = [str(s).strip() for s in raw if str(s).strip()] if isinstance(raw, list) else []
    if scopes:
        out["scopes"] = scopes
    for key in ("valid_from", "valid_until"):
        if fm.get(key):
            out[key] = str(fm[key])
    return out


def _lesson_sections(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _SECTION_RE.finditer(body):
        key = m.group(1).lower()
        if key not in out:
            out[key] = m.group(2).strip()
    return out


def _iter_active_lesson_paths(root: str):
    ldir = paths.lessons_dir(root)
    for p in sorted(glob.glob(os.path.join(ldir, "lesson_*.md"))):
        if os.path.basename(p) == "lesson_template.md":
            continue
        yield p


def _iter_captured_trace_paths(root: str):
    tdir = paths.traces_dir(root)
    for p in sorted(glob.glob(os.path.join(tdir, "*.md"))):
        if os.path.basename(p) == "README.md":
            continue
        if os.path.basename(p).startswith("hub_"):
            continue
        yield p


def _validate_hub_url(hub_url: str) -> None:
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(hub_url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise HubConfigurationError(
            f"refusing to use Hub URL {hub_url!r}: scheme must be http or https, got {scheme or '(none)'!r}"
        )
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if scheme == "http" and hostname not in ("localhost", "127.0.0.1", "::1"):
        raise HubConfigurationError(
            f"refusing to use plaintext http:// for remote Hub URL {hub_url!r}: "
            "the API key is sent as a Bearer token on every call. Use https://, "
            "or connect to localhost/127.0.0.1 for local development."
        )
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ip = ipaddress.IPv4Address(socket.inet_aton(hostname)) if hostname else None
        except (OSError, ValueError):
            ip = None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip is not None and (ip.is_link_local or str(ip) == "fd00:ec2::254"):
        raise HubConfigurationError(
            f"refusing to use Hub URL {hub_url!r}: {hostname} is a link-local or "
            "cloud-metadata address (e.g. 169.254.169.254, fd00:ec2::254) -- "
            "refusing to send the Hub API key there."
        )


class HttpStatusProbe:
    """Records the last non-2xx HTTP response seen on one httpx client."""

    __slots__ = ("status", "retry_after")

    def __init__(self) -> None:
        self.status: int | None = None
        self.retry_after: float | None = None

    def reset(self) -> None:
        self.status = None
        self.retry_after = None

    async def record(self, response) -> None:
        if response.status_code < 400:
            return
        self.status = response.status_code
        raw = response.headers.get("retry-after")
        if raw:
            try:
                self.retry_after = max(0.0, float(str(raw).strip()))
            except ValueError:
                self.retry_after = None


async def _open_session(hub_url: str, api_key: str, timeout_seconds: float):
    _validate_hub_url(hub_url)
    try:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise HubClientUnavailable(
            "the Hub client needs the optional 'mcp' dependency. Install it with "
            "`pip install commontrace[hub-sync]`."
        ) from exc

    probe = HttpStatusProbe()
    http_client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout_seconds,
        event_hooks={"response": [probe.record]},
    )
    return streamable_http_client(hub_url, http_client=http_client), ClientSession, http_client, probe


def _iter_causes(exc: BaseException, _seen: set[int] | None = None):
    _seen = set() if _seen is None else _seen
    if id(exc) in _seen:
        return
    _seen.add(id(exc))
    yield exc
    for nested in getattr(exc, "exceptions", ()) or ():
        yield from _iter_causes(nested, _seen)
    for link in (exc.__cause__, exc.__context__):
        if link is not None:
            yield from _iter_causes(link, _seen)


def root_cause(exc: BaseException) -> BaseException:
    """The most informative exception inside `exc`."""
    leaves = [e for e in _iter_causes(exc) if not getattr(e, "exceptions", None)]
    for leaf in leaves:
        if _http_status(leaf) is not None:
            return leaf
    for leaf in leaves:
        if not isinstance(leaf, HubConnectionError):
            return leaf
    return leaves[0] if leaves else exc


def _http_status(exc: BaseException) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    match = _STATUS_IN_TEXT_RE.search(str(exc))
    return int(match.group(1)) if match else None


def _retry_after_seconds(exc: BaseException) -> float | None:
    for candidate in _iter_causes(exc):
        direct = getattr(candidate, "retry_after", None)
        if isinstance(direct, (int, float)) and direct >= 0:
            return float(direct)
        headers = getattr(getattr(candidate, "response", None), "headers", None)
        if headers is None:
            continue
        try:
            raw = headers.get("retry-after")
        except Exception:  # noqa: BLE001 - a header mapping that misbehaves is not our problem
            continue
        if not raw:
            continue
        try:
            seconds = float(str(raw).strip())
        except ValueError:
            continue
        if seconds >= 0:
            return seconds
    return None


def _is_auth_failure(exc: BaseException, observed_status: int | None = None) -> bool:
    if observed_status in (401, 403):
        return True
    for candidate in _iter_causes(exc):
        if _http_status(candidate) in (401, 403):
            return True
        text = f"{type(candidate).__name__}: {candidate}".lower()
        if any(m in text for m in ("unauthorized", "revoked", "expired api key", "invalid api key")):
            return True
    return False


def _is_rate_limited(exc: BaseException, observed_status: int | None = None) -> bool:
    if observed_status == 429:
        return True
    for candidate in _iter_causes(exc):
        if isinstance(candidate, _ToolRateLimited):
            return True
        if _http_status(candidate) == 429:
            return True
        if "rate_limited" in str(candidate).lower():
            return True
    return False


def _is_retryable(exc: BaseException, observed_status: int | None = None) -> bool:
    if any(isinstance(c, (HubConfigurationError, HubToolError)) for c in _iter_causes(exc)):
        return False
    if _is_auth_failure(exc, observed_status):
        return False
    if _is_rate_limited(exc, observed_status):
        return True
    if isinstance(observed_status, int) and observed_status >= 500:
        return True

    for candidate in _iter_causes(exc):
        status = _http_status(candidate)
        if isinstance(status, int) and status >= 500:
            return True
        if isinstance(candidate, HubConnectionError):
            text = str(candidate).lower()
        else:
            text = f"{type(candidate).__name__}: {candidate}".lower()
        if any(
            marker in text
            for marker in (
                "timeout", "connect", "refused", "reset", "temporarily",
                "eof", "broken pipe", "502", "503", "504",
            )
        ):
            return True
    return False


def _tool_rate_limit(payload: dict) -> _ToolRateLimited | None:
    if not isinstance(payload, dict) or payload.get("error") != "rate_limited":
        return None
    retry_after = payload.get("retry_after")
    try:
        retry_after = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    return _ToolRateLimited(str(payload.get("detail") or "rate_limited"), retry_after)


def _unwrap_result(name: str, result) -> dict:
    if result.is_error:
        text = "; ".join(getattr(c, "text", str(c)) for c in result.content)
        raise HubToolError(f"Hub tool {name!r} returned an error: {text}")
    if result.structured_content is not None:
        return result.structured_content
    text = "".join(getattr(c, "text", "") for c in result.content)
    return json.loads(text) if text else {}


def _already_classified(exc: BaseException) -> HubConnectionError | None:
    for candidate in _iter_causes(exc):
        if isinstance(candidate, HubConnectionError):
            return candidate
    return None


def _transport_failure(
    hub_url: str,
    attempts: int,
    exc: BaseException,
    observed_status: int | None = None,
    observed_retry_after: float | None = None,
) -> HubConnectionError:
    already = _already_classified(exc)
    if already is not None:
        return already
    cause = root_cause(exc)
    detail = f"{type(cause).__name__}: {cause}"
    plural = "attempt" if attempts == 1 else "attempts"
    if _is_auth_failure(exc, observed_status):
        return HubAuthError(
            f"the Hub at {hub_url} rejected the API key (invalid, revoked, or expired). "
            f"Check COMMONTRACE_HUB_API_KEY, or issue a new key with "
            f"`python -m hub.manage issue-key <org_id>`. [{detail}]"
        )
    if _is_rate_limited(exc, observed_status):
        retry_after = _retry_after_seconds(exc) or observed_retry_after
        if any(isinstance(c, _ToolRateLimited) for c in _iter_causes(exc)):
            when = f" Retry after {retry_after:.0f}s." if retry_after else ""
            return HubRateLimited(
                f"the Hub at {hub_url} refused this write for rate limiting and was still "
                f"refusing after {attempts} {plural}.{when} This is the Hub's per-org WRITE "
                f"limit (HUB_RATE_LIMIT_PER_MINUTE, 20/min by default), not a network "
                f"problem -- push a smaller batch, retry later, or raise that limit. "
                f"[{detail}]",
                retry_after=retry_after,
            )
        when = f" Retry after {retry_after:.0f}s." if retry_after else ""
        return HubRateLimited(
            f"the Hub at {hub_url} is rate limiting this client (HTTP 429) and was still "
            f"rate limiting after {attempts} {plural}.{when} Reduce concurrency "
            f"(`--concurrency`), retry later, or raise the Hub's "
            f"HUB_READ_RATE_LIMIT_PER_MINUTE / HUB_RATE_LIMIT_PER_MINUTE. [{detail}]",
            retry_after=retry_after,
        )
    return HubConnectionError(
        f"could not reach the Hub at {hub_url} after {attempts} {plural}: {detail}"
    )


class _RateLimitGate:
    def __init__(self, on_first_pause: Callable[[float], None] | None = None) -> None:
        self._resume_at = 0.0
        self._next_slot = 0.0
        self._min_interval = 0.0
        self._lock = asyncio.Lock()
        self._on_first_pause = on_first_pause
        self._announced = False

    async def wait(self) -> None:
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                if now >= self._resume_at:
                    slot = max(now, self._next_slot)
                    self._next_slot = slot + self._min_interval
                    delay = slot - now
                    break
                delay = self._resume_at - now
            await asyncio.sleep(min(delay, RETRY_MAX_DELAY_SECONDS))
        if delay > 0:
            await asyncio.sleep(delay)

    def pause(self, seconds: float) -> None:
        """Called on a 429: hold the whole batch, and slow the paced rate."""
        loop_time = asyncio.get_running_loop().time()
        resume_at = loop_time + min(max(seconds, 0.0), RETRY_MAX_DELAY_SECONDS)
        self._resume_at = max(self._resume_at, resume_at)
        self._min_interval = min(
            max(self._min_interval * 2.0, _PACE_MIN_INTERVAL_SECONDS), _PACE_MAX_INTERVAL_SECONDS
        )
        self._next_slot = max(self._next_slot, self._resume_at)
        if not self._announced:
            self._announced = True
            if self._on_first_pause is not None:
                self._on_first_pause(seconds)

    def succeeded(self) -> None:
        if self._min_interval:
            self._min_interval *= _PACE_DECAY
            if self._min_interval < _PACE_MIN_INTERVAL_SECONDS / 4:
                self._min_interval = 0.0


class HubSession:
    """One live MCP session, reusable for many tool calls."""

    def __init__(
        self,
        hub_url: str,
        api_key: str,
        timeout_seconds: float,
        max_attempts: int,
        probe: "HttpStatusProbe | None" = None,
        gate: "_RateLimitGate | None" = None,
    ):
        self._hub_url = hub_url
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._probe = probe
        self._gate = gate if gate is not None else _RateLimitGate()
        self._session = None

    async def call(self, name: str, arguments: dict[str, Any]) -> dict:
        """One tool call, with retries."""
        last_exc: Exception | None = None
        attempts = 0
        transport_attempts = 0
        rate_limit_attempts = 0
        status = retry_after = None
        while True:
            attempts += 1
            if self._probe is not None:
                self._probe.reset()
            await self._gate.wait()
            try:
                result = _unwrap_result(name, await self._session.call_tool(name, arguments))
                refusal = _tool_rate_limit(result)
                if refusal is not None:
                    raise refusal
            except (HubToolError, HubAuthError):
                raise
            except Exception as exc:  # noqa: BLE001 - transport errors aren't one exception type
                last_exc = exc
                status = self._probe.status if self._probe is not None else None
                retry_after = self._probe.retry_after if self._probe is not None else None
                rate_limited = _is_rate_limited(exc, status)
                if rate_limited:
                    rate_limit_attempts += 1
                    delay = _backoff_delay(rate_limit_attempts, exc, retry_after)
                    self._gate.pause(delay)
                    budget_left = rate_limit_attempts < RATE_LIMIT_MAX_ATTEMPTS
                else:
                    transport_attempts += 1
                    delay = _backoff_delay(transport_attempts, exc, retry_after)
                    budget_left = transport_attempts < self._max_attempts
                if not budget_left or not _is_retryable(exc, status):
                    break
                await asyncio.sleep(delay)
            else:
                self._gate.succeeded()
                return result
        raise _transport_failure(
            self._hub_url, attempts, last_exc, status, retry_after
        ) from last_exc

    async def search_traces(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        limit: int = 10,
        include_content: bool = True,
    ) -> dict:
        """Search published memory traces matching semantic or keyword query."""
        args: dict[str, Any] = {"query": query, "limit": limit, "include_content": include_content}
        if tags is not None:
            args["tags"] = tags
        return await self.call("search_traces", args)

    async def contribute_trace(
        self,
        text: str,
        *,
        tags: list[str] | None = None,
        agent_type: str = "agent",
        rationale: str = "",
    ) -> dict:
        """Submit a new trace or memory to the Hub Knowledge Base."""
        args: dict[str, Any] = {"text": text, "agent_type": agent_type, "rationale": rationale}
        if tags is not None:
            args["tags"] = tags
        return await self.call("contribute_trace", args)

    async def get_trace(self, trace_id: str) -> dict:
        """Retrieve full details of a specific memory trace by ID."""
        return await self.call("get_trace", {"id": trace_id})

    async def delete_trace(self, trace_id: str) -> dict:
        """Soft-delete or retract a memory trace from the Hub."""
        return await self.call("delete_trace", {"id": trace_id})

    async def vote_trace(self, trace_id: str, vote: str) -> dict:
        """Cast an upvote or downvote on a published memory trace."""
        return await self.call("vote_trace", {"id": trace_id, "vote": vote})

    async def list_tags(self) -> dict:
        """Retrieve all taxonomy tags and their associated trace counts."""
        return await self.call("list_tags", {})

    async def add_comment(self, trace_id: str, body: str) -> dict:
        """Post a review comment or collaborative note on a trace."""
        return await self.call("add_comment", {"trace_id": trace_id, "body": body})

    async def list_comments(self, trace_id: str) -> dict:
        """List all collaborative review comments on a trace."""
        return await self.call("list_comments", {"trace_id": trace_id})

    async def assign_trace(self, trace_id: str, user_id: str) -> dict:
        """Assign trace review ownership to a specific collaborator."""
        return await self.call("assign_trace", {"trace_id": trace_id, "user_id": user_id})

    async def unassign_trace(self, trace_id: str) -> dict:
        """Unassign trace review ownership."""
        return await self.call("unassign_trace", {"trace_id": trace_id})

    async def tag_trace_subjects(self, trace_id: str, subject_ids: list[str]) -> dict:
        """Tag data subject IDs on a trace for privacy/GDPR compliance."""
        return await self.call("tag_trace_subjects", {"id": trace_id, "subject_ids": subject_ids})

    async def purge_subject_traces(self, subject_id: str) -> dict:
        """Purge all traces associated with a specific data subject ID."""
        return await self.call("purge_subject_traces", {"subject_id": subject_id})

    async def commons_overlap(self, failures: list[str]) -> dict:
        """Check known commons failures overlap."""
        return await self.call("commons_overlap", {"failures": failures})


def _backoff_delay(attempt: int, exc: BaseException, observed_retry_after: float | None = None) -> float:
    retry_after = _retry_after_seconds(exc)
    if retry_after is None:
        retry_after = observed_retry_after
    if retry_after is not None:
        return min(max(retry_after, 0.0), RETRY_MAX_DELAY_SECONDS)
    return min(RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), RETRY_MAX_DELAY_SECONDS)


@contextlib.asynccontextmanager
async def open_hub_session(
    hub_url: str,
    api_key: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_first_pause: Callable[[float], None] | None = None,
):
    """Hold one MCP session open for a batch of calls. See HubSession."""
    transport_ctx, ClientSession, http_client, probe = await _open_session(
        hub_url, api_key, timeout_seconds
    )
    hub_session = HubSession(
        hub_url, api_key, timeout_seconds, max_attempts, probe,
        gate=_RateLimitGate(on_first_pause),
    )
    try:
        async with transport_ctx as (read, write), ClientSession(read, write) as session:
            try:
                await session.initialize()
            except Exception as exc:  # noqa: BLE001 - the handshake fails the same ways a call does
                raise _transport_failure(
                    hub_url, 1, exc, probe.status, probe.retry_after
                ) from exc
            hub_session._session = session
            yield hub_session
    finally:
        await http_client.aclose()


async def _call_tool(
    hub_url: str,
    api_key: str,
    name: str,
    arguments: dict[str, Any],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    session: "HubSession | _LazyHubSession | None" = None,
) -> dict:
    if session is not None:
        if isinstance(session, _LazyHubSession):
            session = await session.get()
        return await session.call(name, arguments)

    last_exc: Exception | None = None
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            async with open_hub_session(hub_url, api_key, timeout_seconds, max_attempts=1) as one_shot:
                return await one_shot.call(name, arguments)
        except (HubConfigurationError, HubToolError, HubAuthError, HubClientUnavailable):
            raise
        except HubConnectionError as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            if attempt >= max_attempts:
                break
            await asyncio.sleep(_backoff_delay(attempt, exc))
        except Exception as exc:  # noqa: BLE001 - transport errors aren't one exception type
            last_exc = exc
            classified = _already_classified(exc)
            if isinstance(classified, (HubConfigurationError, HubAuthError, HubToolError)):
                raise classified from exc
            if isinstance(classified, HubRateLimited):
                raise classified from exc
            if attempt >= max_attempts or not _is_retryable(exc):
                break
            await asyncio.sleep(_backoff_delay(attempt, exc))

    raise _transport_failure(hub_url, attempts, last_exc) from last_exc


def _amend_idempotency_key(slug: str, fingerprint: str) -> str:
    return "lesson-amend:" + hashlib.sha256(f"{slug}\x1e{fingerprint}".encode("utf-8")).hexdigest()


def _write_hub_push_fields(path: str, hub_trace_id: str, fingerprint: str) -> None:
    with frontmatter.locked(path):
        fm, body = frontmatter.read(path)
        fm["hub_trace_id"] = hub_trace_id
        fm["hub_pushed_fingerprint"] = fingerprint
        frontmatter.write(path, fm, body)


async def _gather_bounded(
    paths_iter: Iterable[str],
    fn: Callable[[str], Awaitable[_T]],
    concurrency: int = _PUSH_CONCURRENCY,
) -> list[_T]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _bounded(path: str) -> _T:
        async with semaphore:
            return await fn(path)

    return await asyncio.gather(*(_bounded(p) for p in paths_iter))


class _LazyHubSession:
    def __init__(self, stack, hub_url: str, api_key: str, on_first_pause=None):
        self._stack = stack
        self._hub_url = hub_url
        self._api_key = api_key
        self._on_first_pause = on_first_pause
        self._session: HubSession | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> HubSession:
        if self._session is None:
            async with self._lock:
                if self._session is None:
                    self._session = await self._stack.enter_async_context(
                        open_hub_session(
                            self._hub_url, self._api_key, on_first_pause=self._on_first_pause
                        )
                    )
        return self._session


async def _push_batch(
    hub_url: str,
    api_key: str,
    paths_iter: Iterable[str],
    make_push_one: Callable[["_LazyHubSession | None"], Callable[[str], Awaitable[_T]]],
    concurrency: int = _PUSH_CONCURRENCY,
) -> list[_T]:
    files = list(paths_iter)
    if not files:
        return []

    def _announce(seconds: float) -> None:
        print(
            f"[commontrace] the Hub is rate limiting this push; pacing the remaining "
            f"{len(files)} file(s) to match (first wait ~{max(1, round(seconds))}s). "
            f"This is expected for a large batch -- it will finish, just not instantly.",
            file=sys.stderr,
        )

    async with contextlib.AsyncExitStack() as stack:
        session = _LazyHubSession(stack, hub_url, api_key, on_first_pause=_announce)
        return await _gather_bounded(files, make_push_one(session), concurrency)


async def push_active_lessons(
    hub_url: str, api_key: str, root: str, concurrency: int = _PUSH_CONCURRENCY
) -> list[PushResult]:
    async def _push_one(path: str, session: "_LazyHubSession | None" = None) -> PushResult | None:
        try:
            fm, body = frontmatter.read(path)
        except Exception as exc:  # noqa: BLE001 - one malformed local file (hand-edited YAML
            return PushResult(
                slug=os.path.splitext(os.path.basename(path))[0],
                hub_trace_id=None,
                error=f"could not read this file: {type(exc).__name__}: {exc}",
            )
        if fm.get("status") != "active":
            return None
        slug = fm.get("name", os.path.splitext(os.path.basename(path))[0])
        unfilled = templates.unfilled_placeholders(fm, body)
        if unfilled:
            return PushResult(
                slug=slug,
                hub_trace_id=None,
                error=(
                    f"not pushed: this active lesson still contains unedited "
                    f"scaffolding in {', '.join(unfilled)}. Fill it in, or set it "
                    f"back to status: review."
                ),
            )

        sections = _lesson_sections(body)
        solution_text = "\n\n".join(
            part for part in (sections.get("rule", ""), sections.get("how to apply", "")) if part
        ) or "(no Rule/How to apply section found in the lesson body)"
        title = fm.get("description") or slug
        context_text = fm.get("applies_when") or ""
        tags = list(fm.get("tags") or []) if isinstance(fm.get("tags"), list) else []
        fingerprint = _push_fingerprint(title, context_text, solution_text, tags)

        existing_hub_id = fm.get("hub_trace_id")
        if existing_hub_id:
            if fm.get("hub_pushed_fingerprint") == fingerprint:
                return PushResult(slug=slug, hub_trace_id=str(existing_hub_id), skipped=True)
            try:
                result = await _call_tool(
                    hub_url,
                    api_key,
                    "amend_trace",
                    {
                        "id": str(existing_hub_id),
                        "title": title,
                        "context_text": context_text,
                        "solution_text": solution_text,
                        "tags": tags,
                        "idempotency_key": _amend_idempotency_key(slug, fingerprint),
                    },
                    session=session,
                )
            except (HubClientUnavailable, HubConnectionError) as exc:
                return PushResult(slug=slug, hub_trace_id=str(existing_hub_id), error=str(exc))
            if result.get("error"):
                return PushResult(slug=slug, hub_trace_id=str(existing_hub_id), error=result["error"])
            amended_id = result.get("id")
            try:
                await asyncio.to_thread(_write_hub_push_fields, path, amended_id, fingerprint)
            except Exception as exc:  # noqa: BLE001 - the Hub write already succeeded; a
                return PushResult(
                    slug=slug,
                    hub_trace_id=amended_id,
                    error=f"pushed to the Hub but failed to record locally: {type(exc).__name__}: {exc}",
                )
            return PushResult(slug=slug, hub_trace_id=amended_id, quarantined=result.get("quarantined", False))

        try:
            result = await _call_tool(
                hub_url,
                api_key,
                "contribute_trace",
                {
                    "title": title,
                    "context_text": context_text,
                    "solution_text": solution_text,
                    "tags": tags,
                    "agent_type": fm.get("agent_type") or "",
                    "agent_id": fm.get("agent_id") or "",
                    "idempotency_key": f"lesson:{slug}",
                    **_routing_fields(fm),
                },
                session=session,
            )
        except (HubClientUnavailable, HubConnectionError) as exc:
            return PushResult(slug=slug, hub_trace_id=None, error=str(exc))

        if result.get("error"):
            return PushResult(slug=slug, hub_trace_id=None, error=result["error"])

        hub_trace_id = result.get("id")
        try:
            await asyncio.to_thread(_write_hub_push_fields, path, hub_trace_id, fingerprint)
        except Exception as exc:  # noqa: BLE001 - see the amend branch above for why this
            return PushResult(
                slug=slug,
                hub_trace_id=hub_trace_id,
                error=f"pushed to the Hub but failed to record locally: {type(exc).__name__}: {exc}",
            )
        return PushResult(slug=slug, hub_trace_id=hub_trace_id, quarantined=result.get("quarantined", False))

    outcomes = await _push_batch(
        hub_url,
        api_key,
        _iter_active_lesson_paths(root),
        lambda session: (lambda path: _push_one(path, session)),
        concurrency=concurrency,
    )
    return [r for r in outcomes if r is not None]


def _trace_amend_idempotency_key(local_id: str, fingerprint: str) -> str:
    return "trace-amend:" + hashlib.sha256(f"{local_id}\x1e{fingerprint}".encode("utf-8")).hexdigest()


def _trace_filename_suffix(clean_trace_id: str) -> str:
    if len(clean_trace_id) <= 16:
        return clean_trace_id
    digest = hashlib.blake2s(clean_trace_id.encode("utf-8"), digest_size=3).hexdigest()
    return f"{clean_trace_id[:9]}-{digest}"


def _stored_trace_id(path: str) -> str:
    try:
        fm, _ = frontmatter.read(path)
    except Exception:  # noqa: BLE001 - an unreadable neighbour is not this trace
        return ""
    return str(fm.get("id", ""))


async def push_captured_traces(
    hub_url: str, api_key: str, root: str, concurrency: int = _PUSH_CONCURRENCY
) -> list[PushResult]:
    async def _push_one(path: str, session: "_LazyHubSession | None" = None) -> PushResult:
        try:
            instance, _body = trace_io.read(path)
        except Exception as exc:  # noqa: BLE001 - see push_active_lessons's identical
            return PushResult(
                slug=os.path.splitext(os.path.basename(path))[0],
                hub_trace_id=None,
                error=f"could not read this file: {type(exc).__name__}: {exc}",
            )
        local_id = str(instance.get("id") or "")
        slug = local_id or os.path.splitext(os.path.basename(path))[0]
        title = str(instance.get("title") or "")
        context_text = str(instance.get("context_text") or "")
        solution_text = str(instance.get("solution_text") or "")
        tags = list(instance.get("tags") or []) if isinstance(instance.get("tags"), list) else []
        agent_type = str(instance.get("agent_type") or "")
        agent_id = str(instance.get("agent_id") or "")
        profile = str(instance.get("profile") or "")
        outcome = instance.get("outcome") if isinstance(instance.get("outcome"), dict) else {}
        fingerprint = _trace_push_fingerprint(title, context_text, solution_text, tags, outcome)

        existing_hub_id = instance.get("hub_trace_id")
        if existing_hub_id:
            if instance.get("hub_pushed_fingerprint") == fingerprint:
                return PushResult(slug=slug, hub_trace_id=str(existing_hub_id), skipped=True)
            try:
                result = await _call_tool(
                    hub_url,
                    api_key,
                    "amend_trace",
                    {
                        "id": str(existing_hub_id),
                        "title": title,
                        "context_text": context_text,
                        "solution_text": solution_text,
                        "tags": tags,
                        "outcome": outcome,
                        "idempotency_key": _trace_amend_idempotency_key(slug, fingerprint),
                    },
                    session=session,
                )
            except (HubClientUnavailable, HubConnectionError) as exc:
                return PushResult(slug=slug, hub_trace_id=str(existing_hub_id), error=str(exc))
            if result.get("error"):
                return PushResult(slug=slug, hub_trace_id=str(existing_hub_id), error=result["error"])
            amended_id = result.get("id")
            try:
                await asyncio.to_thread(_write_hub_push_fields, path, amended_id, fingerprint)
            except Exception as exc:  # noqa: BLE001 - see push_active_lessons's identical
                return PushResult(
                    slug=slug,
                    hub_trace_id=amended_id,
                    error=f"pushed to the Hub but failed to record locally: {type(exc).__name__}: {exc}",
                )
            return PushResult(slug=slug, hub_trace_id=amended_id, quarantined=result.get("quarantined", False))

        try:
            result = await _call_tool(
                hub_url,
                api_key,
                "contribute_trace",
                {
                    "title": title,
                    "context_text": context_text,
                    "solution_text": solution_text,
                    "tags": tags,
                    "agent_type": agent_type,
                    "agent_id": agent_id,
                    "profile": profile,
                    "outcome": outcome,
                    "idempotency_key": f"trace:{slug}",
                    **_routing_fields(instance),
                },
                session=session,
            )
        except (HubClientUnavailable, HubConnectionError) as exc:
            return PushResult(slug=slug, hub_trace_id=None, error=str(exc))
        if result.get("error"):
            return PushResult(slug=slug, hub_trace_id=None, error=result["error"])

        new_id = result.get("id")
        try:
            await asyncio.to_thread(_write_hub_push_fields, path, new_id, fingerprint)
        except Exception as exc:  # noqa: BLE001 - see the amend branch above for why this
            return PushResult(
                slug=slug,
                hub_trace_id=new_id,
                error=f"pushed to the Hub but failed to record locally: {type(exc).__name__}: {exc}",
            )
        return PushResult(slug=slug, hub_trace_id=new_id, quarantined=result.get("quarantined", False))

    return await _push_batch(
        hub_url,
        api_key,
        _iter_captured_trace_paths(root),
        lambda session: (lambda path: _push_one(path, session)),
        concurrency=concurrency,
    )


async def commons_overlap(
    hub_url: str,
    api_key: str,
    failures: list[dict],
    threshold: float | None = None,
    include_matches: bool = True,
) -> dict:
    arguments: dict[str, Any] = {"failures": failures, "include_matches": include_matches}
    if threshold is not None:
        arguments["threshold"] = threshold
    response = await _call_tool(hub_url, api_key, "commons_overlap", arguments)
    if response.get("error"):
        raise HubConnectionError(f"commons_overlap failed: {response['error']}: {response.get('detail', '')}")
    return response


async def commons_search(
    hub_url: str,
    api_key: str,
    query_signature: list[int],
    limit: int | None = None,
    agent_type: str = "",
) -> dict:
    """Ask the Hub what it already knows about ONE failure, ranked."""
    arguments: dict[str, Any] = {"query_signature": query_signature}
    if limit is not None:
        arguments["limit"] = limit
    if agent_type:
        arguments["agent_type"] = agent_type
    response = await _call_tool(hub_url, api_key, "commons_search", arguments)
    if response.get("error"):
        raise HubConnectionError(f"commons_search failed: {response['error']}: {response.get('detail', '')}")
    return response


async def commons_export(hub_url: str, api_key: str, limit: int | None = None) -> dict:
    """Fetch the operator's curated Knowledge Base corpus for local matching."""
    arguments: dict[str, Any] = {}
    if limit is not None:
        arguments["limit"] = limit
    response = await _call_tool(hub_url, api_key, "commons_export", arguments)
    if response.get("error"):
        raise HubConnectionError(
            f"commons_export failed: {response['error']}: {response.get('detail', '')}")
    return response


async def submit_kb_entry(
    hub_url: str,
    api_key: str,
    title: str,
    context_text: str,
    solution_text: str,
    tags: list[str] | None = None,
    agent_type: str = "",
    rationale: str = "",
    idempotency_key: str | None = None,
) -> dict:
    arguments: dict[str, Any] = {
        "title": title, "context_text": context_text, "solution_text": solution_text,
        "tags": tags or [], "agent_type": agent_type, "rationale": rationale,
    }
    if idempotency_key is not None:
        arguments["idempotency_key"] = idempotency_key
    response = await _call_tool(hub_url, api_key, "submit_kb_entry", arguments)
    if response.get("error"):
        raise HubConnectionError(f"submit_kb_entry failed: {response['error']}: {response.get('detail', '')}")
    return response


async def list_my_kb_submissions(hub_url: str, api_key: str, limit: int | None = None) -> dict:
    """This org's own Knowledge Base submissions and their review status."""
    arguments: dict[str, Any] = {}
    if limit is not None:
        arguments["limit"] = limit
    response = await _call_tool(hub_url, api_key, "list_my_kb_submissions", arguments)
    if response.get("error"):
        raise HubConnectionError(
            f"list_my_kb_submissions failed: {response['error']}: {response.get('detail', '')}"
        )
    return response


async def account_usage(hub_url: str, api_key: str) -> dict:
    """What this org's plan entitles it to, and what it has used."""
    response = await _call_tool(hub_url, api_key, "account_usage", {})
    if response.get("error"):
        raise HubConnectionError(f"account_usage failed: {response['error']}: {response.get('detail', '')}")
    return response


async def fleet_outcomes(hub_url: str, api_key: str, agent_type: str = "") -> dict:
    arguments: dict = {}
    if agent_type:
        arguments["agent_type"] = agent_type
    response = await _call_tool(hub_url, api_key, "fleet_outcomes", arguments)
    if response.get("error"):
        raise HubConnectionError(
            f"fleet_outcomes failed: {response['error']}: {response.get('detail', '')}"
        )
    return response


async def holdout_assign(
    hub_url: str, api_key: str, trace_ids: list[str], occasion_id: str
) -> dict:
    response = await _call_tool(
        hub_url, api_key, "holdout_assign",
        {"trace_ids": list(trace_ids), "occasion_id": occasion_id},
    )
    if response.get("error"):
        raise HubConnectionError(
            f"holdout_assign failed: {response['error']}: {response.get('detail', '')}"
        )
    return response


async def record_occasion_outcome(
    hub_url: str, api_key: str, occasion_id: str, succeeded: bool
) -> dict:
    response = await _call_tool(
        hub_url, api_key, "record_occasion_outcome",
        {"occasion_id": occasion_id, "succeeded": bool(succeeded)},
    )
    if response.get("error"):
        raise HubConnectionError(
            f"record_occasion_outcome failed: {response['error']}: "
            f"{response.get('detail', '')}"
        )
    return response


async def delete_trace(hub_url: str, api_key: str, trace_id: str) -> bool:
    response = await _call_tool(hub_url, api_key, "delete_trace", {"id": trace_id})
    if response.get("error") == "not_found":
        return False
    if response.get("error"):
        raise HubConnectionError(f"delete_trace failed: {response['error']}: {response.get('detail', '')}")
    return bool(response.get("deleted"))


async def request_account_deletion(hub_url: str, api_key: str) -> dict:
    response = await _call_tool(hub_url, api_key, "request_account_deletion", {})
    if response.get("error"):
        raise HubConnectionError(
            f"request_account_deletion failed: {response['error']}: {response.get('detail', '')}"
        )
    return response


async def cancel_account_deletion(hub_url: str, api_key: str) -> bool:
    response = await _call_tool(hub_url, api_key, "cancel_account_deletion", {})
    if response.get("error"):
        raise HubConnectionError(
            f"cancel_account_deletion failed: {response['error']}: {response.get('detail', '')}"
        )
    return bool(response.get("cancelled"))


async def confirm_account_deletion(hub_url: str, api_key: str, confirmation_token: str) -> None:
    response = await _call_tool(
        hub_url, api_key, "confirm_account_deletion", {"confirmation_token": confirmation_token}
    )
    if response.get("error"):
        raise HubConnectionError(
            f"confirm_account_deletion failed: {response['error']}: {response.get('detail', '')}"
        )


DEFAULT_MAX_PULL_RESULTS = 2000


async def pull_search_results(
    hub_url: str,
    api_key: str,
    root: str,
    query: str = "",
    tags: list[str] | None = None,
    max_results: int = DEFAULT_MAX_PULL_RESULTS,
) -> PullResult:
    traces: list[dict] = []
    ignored_terms: list[str] = []
    offset = 0
    while True:
        response = await _call_tool(
            hub_url, api_key, "search_traces", {"query": query, "tags": tags or [], "offset": offset}
        )
        if response.get("error"):
            raise HubConnectionError(f"search_traces failed: {response['error']}")
        raw_ignored = response.get("terms_ignored")
        if isinstance(raw_ignored, list):
            ignored_terms = [str(t) for t in raw_ignored]
        page = response.get("traces", [])
        traces.extend(page)
        if not page or not response.get("has_more") or len(traces) >= max_results:
            break
        offset = int(response.get("offset", offset)) + (int(response.get("limit", 0)) or len(page))
    if len(traces) > max_results:
        traces = traces[:max_results]

    tdir = paths.traces_dir(root)
    os.makedirs(tdir, exist_ok=True)
    tdir_abs = os.path.abspath(tdir)

    written: list[str] = []
    for trace in traces:
        raw_trace_id = str(trace.get("id", "")) if trace.get("id") is not None else ""
        clean_trace_id = re.sub(r"[^A-Za-z0-9_-]", "", raw_trace_id)[:64]
        raw_title = str(trace.get("title") or "trace")
        slug = re.sub(r"[^a-z0-9]+", "-", raw_title.lower()).strip("-")[:60] or "trace"
        suffix = _trace_filename_suffix(clean_trace_id)
        filename = f"hub_{slug}_{suffix}.md" if clean_trace_id else f"hub_{slug}.md"
        out_path = os.path.abspath(os.path.join(tdir_abs, filename))
        if not (out_path == tdir_abs or out_path.startswith(tdir_abs + os.sep)):
            raise ValueError(f"Path traversal detected in trace id: {raw_trace_id!r}")
        if os.path.exists(out_path) and _stored_trace_id(out_path) == clean_trace_id:
            continue

        fm = templates.trace_frontmatter(
            clean_trace_id or slug,
            str(trace.get("title") or ""),
            str(trace.get("agent_type") or ""),
            list(trace.get("tags") or []) if isinstance(trace.get("tags"), (list, tuple)) else [],
            str(trace.get("profile") or ""),
            trace.get("outcome") if isinstance(trace.get("outcome"), dict) else None,
            scopes=list(trace.get("scopes") or []) if isinstance(trace.get("scopes"), (list, tuple)) else None,
            valid_from=str(trace.get("valid_from") or ""),
            valid_until=str(trace.get("valid_until") or ""),
            expires_at=str(trace.get("expires_at") or ""),
        )
        fm["hub_trace_id"] = raw_trace_id
        body = templates.trace_body(trace.get("context_text") or "", trace.get("solution_text") or "")
        frontmatter.write(out_path, fm, body)
        written.append(out_path)

    return PullResult(written_paths=written, n_found=len(traces), ignored_terms=ignored_terms)
