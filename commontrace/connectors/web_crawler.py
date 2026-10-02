"""Web connector: fetch seed pages (robots.txt honoured) into facts with provenance."""
from __future__ import annotations

import html as _html
import re
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
)
from commontrace.ingest import Chunk, IngestionResult, _fingerprint, _redact_secrets

STATE_KEY = "web_crawler:default"
MAX_PAGE_BYTES = 512 * 1024
MAX_ROBOTS_BYTES = 64 * 1024
MAX_VISITED = 10_000


def _is_http_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_http_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect to a non-http(s) URL refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpOnlyRedirects)


def _get(url: str, user_agent: str, timeout: float, limit: int) -> tuple[str, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with _OPENER.open(req, timeout=timeout) as resp:  # nosec B310 - http(s) only, checked above
        ctype = str(resp.headers.get("Content-Type", "") or "")
        return ctype, resp.read(limit)


def _robots_allowed(url: str, user_agent: str, timeout: float) -> bool:
    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        _ctype, raw = _get(robots_url, user_agent, timeout, MAX_ROBOTS_BYTES)
    except urllib.error.HTTPError as exc:
        return exc.code not in (401, 403)
    except (OSError, ValueError):
        return True
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(raw.decode("utf-8", errors="replace").splitlines())
    return bool(parser.can_fetch(user_agent or "*", url))


def _strip_tags(fragment: str) -> str:
    return re.sub(r"<[^>]+>", "", fragment)


def _html_to_markdown_text(html_text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1\s*>", "\n", html_text)
    text = re.sub(r"(?is)<!--.*?-->", "", text)
    text = re.sub(r"(?is)<\s*h([1-3])[^>]*>(.*?)</\s*h\1\s*>",
                  lambda m: "\n%s %s\n" % ("#" * int(m.group(1)), _strip_tags(m.group(2)).strip()), text)
    text = re.sub(r"(?i)<\s*(p|div|section|article|br|li|tr)[^>]*>", "\n", text)
    text = _html.unescape(_strip_tags(text))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


class WebCrawlerConnector(Connector):
    """Fetch a set of seed URLs; each URL is fetched once until its state is reset."""

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
        limit = max(0, int(max_pages if max_pages is not None else self.max_pages))
        if timeout is not None:
            self.timeout = int(timeout)
        result = IngestionResult(source_path=",".join(seeds), source_type="web_crawler")

        visited = resolve_cursor(root, STATE_KEY, state_token).get("visited", [])
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

        new_visited = (visited + [t for t, _c in fetched])[-MAX_VISITED:]
        new_cursor = {"visited": new_visited}
        new_token = encode_state_token(new_cursor)
        if not dry_run:
            for target, page_chunks in fetched:
                record_chunks(root, page_chunks, source_id=f"web:{target}", scope=active_scope,
                              run_id=rid, connector=self.name, result=result)
            try:
                save_connector_state(root, STATE_KEY, new_token, new_cursor)
            except OSError as exc:
                result.errors.append("state persist error: %s" % exc)
        return SyncResult(connector=self.name, chunks=chunks, result=result, new_state_token=new_token,
                          run_id=rid, dry_run=dry_run, pending=max(0, len(pending) - limit))
