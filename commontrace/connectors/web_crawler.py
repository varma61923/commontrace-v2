"""Web-crawler connector: robots-respecting stdlib fetch -> markdown chunker.

Fetches ``http(s)`` URLs with :mod:`urllib` only, honours ``robots.txt`` via
:mod:`urllib.robotparser`, converts HTML to heading-preserving text, chunks
with the shared markdown chunker (secret redaction included), writes atomic
facts + provenance (unless dry-run), and checkpoints visited URLs in a
git-tracked JSON state token.
"""
from __future__ import annotations

import html as _html
import re
import urllib.parse
import urllib.request
import urllib.robotparser
from typing import Any

from commontrace.connectors.base import (
    Connector,
    SyncResult,
    categorize_chunk,
    chunk_markdown_text,
    decode_state_token,
    encode_state_token,
    load_connector_state,
    new_run_id,
    register_connector,
    save_connector_state,
)
from commontrace.ingest import IngestionResult, _redact_secrets

STATE_KEY = "web_crawler:default"
_MAX_BYTES = 512 * 1024


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return "%s://%s" % (parts.scheme, parts.netloc)


def _is_http_url(url: str) -> bool:
    try:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
    except Exception:
        return False
    return scheme in ("http", "https")


def _robots_allowed(url: str, user_agent: str, timeout: int) -> bool:
    """True when fetching ``url`` is allowed (fail-open on robots errors)."""
    try:
        origin = _origin(url)
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(origin + "/robots.txt")
        parser.read()
        try:
            return bool(parser.can_fetch(user_agent or "*", url))
        except Exception:
            return True
    except Exception:
        return True


def _html_to_markdown_text(html_text: str) -> str:
    """Convert HTML to heading-preserving plain text using stdlib only."""
    text = html_text if isinstance(html_text, str) else str(html_text)
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*h([1-3])[^>]*>(.*?)</\s*h\1\s*>",
                  lambda m: "\n%s %s\n" % ("#" * int(m.group(1)), _strip_tags(m.group(2))),
                  text)
    text = re.sub(r"(?i)<\s*(p|div|section|article|br|li|tr)[^>]*>", "\n", text)
    text = _strip_tags(text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _strip_tags(fragment: str) -> str:
    return re.sub(r"<[^>]+>", "", fragment)


@register_connector
class WebCrawlerConnector(Connector):
    """Crawl a small set of seed URLs into markdown chunks."""

    name = "web_crawler"

    def __init__(
        self,
        scope: str = "",
        max_pages: int = 20,
        timeout: int = 10,
        user_agent: str = "CommonTraceBot/1.0 (+https://commontrace.local)",
    ):
        self.scope = scope
        self.max_pages = max_pages
        self.timeout = timeout
        self.user_agent = user_agent

    def authorize(self, credentials: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ok": True, "connector": self.name}

    # -- fetch pipeline (split for test monkeypatching) ------------------
    def _allowed(self, url: str) -> bool:
        return _robots_allowed(url, self.user_agent, self.timeout)

    def _fetch(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            ctype = ""
            try:
                ctype = str(resp.headers.get("Content-Type", ""))
            except Exception:
                ctype = ""
            if ctype and "text" not in ctype.lower() and "html" not in ctype.lower():
                raise ValueError("unsupported content-type: %s" % ctype)
            raw = resp.read(_MAX_BYTES)
        text = raw.decode("utf-8", errors="replace")
        return _redact_secrets(text)

    def _fetch_text(self, url: str) -> str:
        html_text = self._fetch(url)
        markdownish = _html_to_markdown_text(html_text)
        # Belt-and-braces: redact again after tag stripping/unescaping.
        return _redact_secrets(markdownish)

    def _chunk_url(self, url: str, text: str) -> list:
        chunks = chunk_markdown_text(text, url)
        if chunks:
            return chunks
        # Fallback so short pages still produce evidence (redacted).
        body = _redact_secrets(text.strip())
        if len(body) < 50:
            return []
        from commontrace.connectors.base import _fingerprint
        from commontrace.ingest import Chunk

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
        from commontrace import hierarchical
        from commontrace import provenance as _prov

        seeds: list[str] = []
        if urls:
            seeds.extend(urls)
        if url:
            seeds.append(url)
        extra = kwargs.get("sources") or kwargs.get("seeds") or []
        if isinstance(extra, str):
            extra = [extra]
        seeds.extend(list(extra or []))
        # Deduplicate, preserving order.
        seen: set[str] = set()
        ordered: list[str] = []
        for u in seeds:
            u = str(u).strip()
            if u and u not in seen:
                seen.add(u)
                ordered.append(u)

        rid = run_id or new_run_id()
        active_scope = scope if scope != "" else self.scope
        limit = int(max_pages if max_pages is not None else self.max_pages)
        if timeout is not None:
            self.timeout = int(timeout)
        result = IngestionResult(source_path=",".join(ordered), source_type="web_crawler")

        cursor: dict[str, Any] = {}
        if state_token:
            cursor = decode_state_token(state_token)
        if not cursor:
            entry = load_connector_state(root, STATE_KEY)
            if entry.get("state_token") and not state_token:
                cursor = decode_state_token(entry.get("state_token", ""))
            if not cursor and isinstance(entry.get("cursor"), dict):
                cursor = dict(entry["cursor"])
        visited = cursor.get("visited", [])
        visited_set = set(visited) if isinstance(visited, list) else set()

        pending = [u for u in ordered if u not in visited_set][:limit]
        chunks: list = []
        newly_visited: list[str] = list(visited) if isinstance(visited, list) else []

        for target in pending:
            if not _is_http_url(target):
                result.errors.append("web_crawler: skipping non-http(s) URL: %r" % target)
                continue
            try:
                if not self._allowed(target):
                    result.errors.append("web_crawler: disallowed by robots.txt: %r" % target)
                    continue
            except Exception as exc:
                result.errors.append("web_crawler: robots check failed for %r: %s" % (target, exc))
                continue
            try:
                text = self._fetch_text(target)
            except Exception as exc:
                result.errors.append("web_crawler: fetch failed for %r: %s" % (target, exc))
                continue
            try:
                page_chunks = self._chunk_url(target, text)
            except Exception as exc:
                result.errors.append("web_crawler: chunk failed for %r: %s" % (target, exc))
                continue
            chunks.extend(page_chunks)
            newly_visited.append(target)
            if not dry_run:
                for chunk in page_chunks:
                    statement = "%s: %s" % (chunk.breadcrumb, chunk.content[:200])
                    statement = statement.strip()
                    if len(statement) > 30:
                        try:
                            hierarchical.add_fact(
                                root,
                                statement=statement[:500],
                                category=categorize_chunk(chunk.breadcrumb),
                                scopes=[active_scope] if active_scope else None,
                                confidence=0.7,
                            )
                            result.facts_written += 1
                        except Exception as exc:
                            result.errors.append("fact error: %s" % exc)
                    try:
                        _prov.append_provenance(
                            root,
                            target_kind="chunk",
                            target_id=chunk.chunk_id,
                            source_path=chunk.source_path,
                            run_id=rid,
                            detail={
                                "connector": self.name,
                                "breadcrumb": chunk.breadcrumb,
                                "chunk_type": chunk.chunk_type,
                            },
                        )
                    except Exception as exc:
                        result.errors.append("provenance error: %s" % exc)

        result.chunks_extracted = len(chunks)
        new_cursor = {"visited": newly_visited}
        new_token = encode_state_token(new_cursor)
        if not dry_run:
            try:
                save_connector_state(root, STATE_KEY, new_token, new_cursor)
            except Exception as exc:
                result.errors.append("state persist error: %s" % exc)
        return SyncResult(
            connector=self.name, chunks=chunks, result=result,
            new_state_token=new_token, run_id=rid, dry_run=dry_run,
        )

    def webhook_handler(self, root: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"ok": False, "connector": self.name, "reason": "payload must be a dict"}
        url = str(payload.get("url", "")).strip()
        if not url or not _is_http_url(url):
            return {"ok": False, "connector": self.name, "reason": "missing/invalid 'url'"}
        return {"ok": True, "connector": self.name, "queued": url}
