"""Web connector: fetch seed pages (robots.txt honoured) into facts with provenance."""
from __future__ import annotations

import html as _html
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from typing import Any

from commontrace.connectors.base import (
    Connector,
    SyncResult,
    chunk_markdown_text,
    encode_state_token,
    new_run_id,
    record_chunks,
    resolve_cursor,
    save_connector_state,
    scoped_key,
)
from commontrace.ingest import Chunk, IngestionResult, _fingerprint, _redact_secrets

STATE_KEY = "web_crawler:default"
MAX_PAGE_BYTES = 512 * 1024
MAX_ROBOTS_BYTES = 64 * 1024
MAX_VISITED = 10_000


def _is_http_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        # Accessing port also validates malformed or out-of-range port values.
        _ = parts.port
        return (
            parts.scheme.lower() in ("http", "https")
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not any(ord(char) <= 32 or ord(char) == 127 for char in url)
        )
    except ValueError:
        return False


def _redirect_target_is_public(url: str) -> bool:
    """Check all DNS answers; a redirect must not introduce an internal target.

    Explicit intranet/loopback seeds remain supported. This guards cross-origin
    redirects, not DNS rebinding: network egress policy is still required when
    crawling untrusted domains in a privileged network.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return False
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        answers = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                     type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(answer[4][0].split("%", 1)[0]) for answer in answers]
    return bool(addresses) and all(
        address.is_global and not address.is_multicast
        and not getattr(address, "is_site_local", False) for address in addresses
    )


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from commontrace import offline

        if not _is_http_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect to a non-http(s) URL refused", headers, fp)
        offline.check_url(newurl, "web crawling redirect")
        source = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if (source.hostname, source.port) != (target.hostname, target.port):
            if not _redirect_target_is_public(newurl):
                raise ValueError("redirect to a non-public network address refused")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and (source.scheme, source.netloc) != (target.scheme, target.netloc):
            # urllib copies arbitrary headers across origins. Never forward credentials.
            for name in ("Authorization", "Proxy-authorization", "Cookie"):
                redirected.remove_header(name)
        return redirected


_OPENER = urllib.request.build_opener(_HttpOnlyRedirects)


def _get(url: str, user_agent: str, timeout: float, limit: int) -> tuple[str, bytes]:
    from commontrace import offline

    offline.check_url(url, "web crawling")
    if not _is_http_url(url):
        raise ValueError("web crawling requires an http(s) URL without embedded credentials")
    if limit <= 0:
        raise ValueError("web crawling response size limit must be positive")
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with _OPENER.open(req, timeout=timeout) as resp:  # nosec B310 - http(s) only, checked above
        ctype = str(resp.headers.get("Content-Type", "") or "")
        raw = resp.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("web crawling response exceeds size limit")
        return ctype, raw


def _robots_allowed(url: str, user_agent: str, timeout: float) -> bool:
    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        _ctype, raw = _get(robots_url, user_agent, timeout, MAX_ROBOTS_BYTES)
    except urllib.error.HTTPError as exc:
        return exc.code not in (401, 403)
    except ValueError:
        # Truncated robots rules must never silently turn a disallow into allow.
        return False
    except OSError:
        return True
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(raw.decode("utf-8", errors="replace").splitlines())
    return bool(parser.can_fetch(user_agent or "*", url))


# Pages are untrusted and up to MAX_PAGE_BYTES long. The previous regex
# conversion (`<(script|...)[^>]*>.*?</\1>`, `<!--.*?-->`, `<[^>]+>`) rescanned
# to the end of the page from every unclosed opener, so a page of repeated
# `<script>` or `<!--` took minutes. This scanner visits each character a
# bounded number of times. `[^<>]*` stops at the next `<`, and each closing
# tag is searched for at most once per position (see _Closers).
_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)[^<>]*>")
# <!DOCTYPE>, <?xml?>, and malformed closers such as `</ h1>`: markup, not text.
_DECLARATION_RE = re.compile(r"<[!?/][^<>]*>")
# ASCII-only lowering keeps every offset aligned with the original text;
# str.lower() can change the length (e.g. "\u0130" lowers to two code points).
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_DROPPED_ELEMENTS = frozenset({"script", "style", "noscript"})
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3}
_BLOCK_TAGS = frozenset({"p", "div", "section", "article", "br", "li", "tr"})


class _Closers:
    """Where `</name` next occurs, found with one forward search per element.

    Lookups only ever move forward, so a cached hit ahead of the cursor is
    reused and a miss is final: no position is searched twice per element.
    """

    def __init__(self, lowered: str) -> None:
        self._lowered = lowered
        self._next: dict[str, int] = {}

    def find(self, name: str, start: int) -> int:
        cached = self._next.get(name)
        if cached is not None and (cached < 0 or cached >= start):
            return cached
        found = self._lowered.find("</" + name, start)
        self._next[name] = found
        return found


def _convert(text: str, closers: _Closers, start: int, end: int, *, outer: bool) -> list[str]:
    out: list[str] = []
    pos = start
    while pos < end:
        lt = text.find("<", pos, end)
        if lt < 0:
            out.append(text[pos:end])
            break
        out.append(text[pos:lt])
        if text.startswith("<!--", lt):
            close = text.find("-->", lt + 4, end)
            pos = end if close < 0 else close + 3
            continue
        match = _TAG_RE.match(text, lt, end)
        if match is None:
            declaration = _DECLARATION_RE.match(text, lt, end)
            if declaration is not None:
                pos = declaration.end()
            else:
                out.append("<")
                pos = lt + 1
            continue
        closing, name = match.group(1), match.group(2).lower()
        pos = match.end()
        if closing:
            continue
        if name in _DROPPED_ELEMENTS:
            # An unclosed script/style runs to the end of the document, as in a browser.
            close = closers.find(name, pos)
            if close < 0 or close >= end:
                pos = end
            else:
                gt = text.find(">", close, end)
                pos = end if gt < 0 else gt + 1
            if outer:
                out.append("\n")
            continue
        level = _HEADINGS.get(name)
        if outer and level is not None:
            close = closers.find(name, pos)
            if 0 <= close < end:
                inner = "".join(_convert(text, closers, pos, close, outer=False)).strip()
                out.append("\n%s %s\n" % ("#" * level, inner))
                gt = text.find(">", close, end)
                pos = end if gt < 0 else gt + 1
            continue
        if outer and name in _BLOCK_TAGS:
            out.append("\n")
    return out


def _strip_tags(fragment: str) -> str:
    closers = _Closers(fragment.translate(_ASCII_LOWER))
    return "".join(_convert(fragment, closers, 0, len(fragment), outer=False))


def _html_to_markdown_text(html_text: str) -> str:
    closers = _Closers(html_text.translate(_ASCII_LOWER))
    text = _html.unescape("".join(_convert(html_text, closers, 0, len(html_text), outer=True)))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


class WebCrawlerConnector(Connector):
    """Fetch seeds once per scope until state is reset.

    Explicit local/intranet seeds are supported. Cross-origin redirects cannot
    introduce non-public destinations. Use network egress controls when accepting
    untrusted seeds; DNS rebinding and initial private seeds are not blocked here.
    """

    name = "web_crawler"

    def __init__(
        self,
        scope: str = "",
        max_pages: int = 20,
        timeout: int = 10,
        user_agent: str = "CommonTraceBot/1.0",
    ):
        self.scope = scope
        self.max_pages = max_pages
        self.timeout = timeout
        self.user_agent = user_agent

    def _allowed(self, url: str) -> bool:
        return _robots_allowed(url, self.user_agent, self.timeout)

    def _fetch(self, url: str) -> str:
        ctype, raw = _get(url, self.user_agent, self.timeout, MAX_PAGE_BYTES)
        if ctype and not re.search(r"text/|html|xml|markdown", ctype, re.I):
            raise ValueError("unsupported content-type: %s" % ctype)
        charset = re.search(r"charset=([\w\-]+)", ctype or "", re.I)
        try:
            text = raw.decode(charset.group(1) if charset else "utf-8", errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        return _redact_secrets(text)

    def _fetch_text(self, url: str) -> str:
        return _redact_secrets(_html_to_markdown_text(self._fetch(url)))

    @staticmethod
    def _chunk_url(url: str, text: str) -> list[Chunk]:
        chunks = chunk_markdown_text(text, url)
        if chunks:
            return chunks
        body = text.strip()
        if len(body) < 50:
            return []
        return [Chunk(
            content=body[:2000],
            source_path=url,
            chunk_id="%s_page" % _fingerprint(url),
            breadcrumb=urllib.parse.urlsplit(url).path or url,
            chunk_type="web_page",
        )]

    def sync(
        self,
        root: str,
        state_token: str | None = None,
        *,
        scope: str = "",
        run_id: str = "",
        dry_run: bool = False,
        urls: list[str] | None = None,
        url: str | None = None,
        max_pages: int | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> SyncResult:
        seeds = list(dict.fromkeys(
            str(u).strip() for u in [*(urls or []), *([url] if url else [])] if str(u).strip()
        ))
        rid = run_id or new_run_id()
        active_scope = scope or self.scope
        state_key = scoped_key(STATE_KEY, active_scope)
        limit = max(0, int(max_pages if max_pages is not None else self.max_pages))
        if timeout is not None:
            self.timeout = int(timeout)
        result = IngestionResult(source_path=",".join(seeds), source_type="web_crawler")

        visited = resolve_cursor(root, state_key, state_token, scope=active_scope).get("visited", [])
        visited = [v for v in visited if isinstance(v, str)] if isinstance(visited, list) else []
        seen = set(visited)
        pending = [u for u in seeds if u not in seen]
        chunks: list[Chunk] = []
        fetched: list[tuple[str, list[Chunk]]] = []
        for target in pending[:limit]:
            if not _is_http_url(target):
                result.errors.append("web_crawler: skipping non-http(s) URL: %r" % target)
                continue
            try:
                if not self._allowed(target):
                    result.errors.append("web_crawler: disallowed by robots.txt: %r" % target)
                    continue
                page_chunks = self._chunk_url(target, self._fetch_text(target))
            except (OSError, ValueError) as exc:
                result.errors.append("web_crawler: fetch failed for %r: %s" % (target, exc))
                continue
            chunks.extend(page_chunks)
            fetched.append((target, page_chunks))
        result.chunks_extracted = len(chunks)

        completed = [t for t, _c in fetched] if dry_run else []
        if not dry_run:
            for target, page_chunks in fetched:
                if record_chunks(root, page_chunks, source_id=f"web:{target}",
                                 scope=active_scope, run_id=rid, connector=self.name, result=result):
                    completed.append(target)
        new_visited = (visited + completed)[-MAX_VISITED:]
        new_cursor = {"visited": new_visited, "scope": active_scope, "connector_key": state_key}
        new_token = encode_state_token(new_cursor)
        if not dry_run:
            try:
                save_connector_state(root, state_key, new_token, new_cursor)
            except OSError as exc:
                result.errors.append("state persist error: %s" % exc)
        return SyncResult(connector=self.name, chunks=chunks, result=result, new_state_token=new_token,
                          run_id=rid, dry_run=dry_run, pending=max(0, len(pending) - len(completed)))
