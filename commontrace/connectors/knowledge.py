"""Read-only provider connectors with pinned hosts and resumable, failure-safe watermarks.

OAuth tokens are supplied by the operator; provider redirects never carry them.
Provider text always enters with external authority and an owner-pinned scope.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request

from commontrace import _jsonl, additive_extract, hierarchical, memory_authority, paths, trace_io

# Exact provider origin and read endpoint; resource identifiers are URL encoded.
PROVIDERS = {
    "github": ("https://api.github.com", "/repos/{resource}/issues?state=all&per_page=100"),
    "slack": ("https://slack.com", "/api/conversations.history?channel={resource}&limit=100"),
    "drive": ("https://www.googleapis.com", "/drive/v3/files/{resource}?fields=mimeType,name"),
    "gmail": ("https://gmail.googleapis.com", "/gmail/v1/users/me/messages/{resource}?format=full"),
    "notion": ("https://api.notion.com", "/v1/blocks/{resource}/children?page_size=100"),
    "onedrive": ("https://graph.microsoft.com",
                 "/v1.0/me/drive/items/{resource}?$select=name,@microsoft.graph.downloadUrl"),
    "intercom": ("https://api.intercom.io", "/conversations/{resource}"),
    "greenhouse": ("https://harvest.greenhouse.io", "/v1/candidates/{resource}"),
    "confluence": ("https://api.atlassian.com",
                   "/ex/confluence/{resource}/wiki/api/v2/pages?limit=100&body-format=storage"),
    "jira": ("https://api.atlassian.com", "/ex/jira/{resource}/rest/api/3/search/jql?maxResults=100"),
    "zendesk": ("https://{resource}.zendesk.com", "/api/v2/help_center/articles.json?per_page=100"),
    "linear": ("https://api.linear.app", "/graphql"),
    "servicenow": ("https://{resource}.service-now.com",
                   "/api/now/table/incident?sysparm_limit=100&sysparm_offset=0"),
    "salesforce": ("https://{resource}.my.salesforce.com",
                   "/services/data/v65.0/query?q=SELECT+Id,Subject,Description+FROM+Case"),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("provider redirect refused; bearer credentials remain on their pinned origin")


def _text(value) -> str:
    """Provider JSON fields containing actual knowledge, never credential metadata."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(item) for item in value)))
    if not isinstance(value, dict):
        return ""
    # Native rich text, discussion bodies and connector-specific envelopes.
    texts = []
    for key in ("title", "body", "text", "plain_text", "description", "content", "rich_text",
                "messages", "results", "articles", "issues", "conversation_parts", "parts", "fields", "value",
                "storage", "atlas_doc_format", "summary", "Subject", "Description", "short_description",
                "result", "records", "data", "nodes"):
        if key in value:
            texts.append(_text(value[key]))
    # Notion block type selects its exact rich-text content.
    block = value.get("type")
    if isinstance(block, str) and block in value and isinstance(value[block], dict):
        texts.append(_text(value[block]))
    return "\n".join(filter(None, texts))


def _gmail(payload) -> str:
    import base64

    result = []
    def part(value):
        if not isinstance(value, dict):
            return
        data = value.get("body", {}).get("data")
        if isinstance(data, str) and value.get("mimeType") in ("text/plain", "text/html"):
            result.append(base64.urlsafe_b64decode(data+"="*((-len(data)) % 4)).decode("utf-8", "replace"))
        for child in value.get("parts", []):
            part(child)
    part(payload.get("payload", {}))
    return "\n".join(result) or payload.get("snippet", "")


def sync(root: str, provider: str, resource: str, *, token: str, context: list[str],
         max_pages: int = 10, fetch=None, replace_snapshot: bool = False) -> dict:
    if provider not in PROVIDERS or not isinstance(resource, str) or not 1 <= len(resource) <= 512 or not token:
        raise ValueError("known provider, bounded resource and operator OAuth token required")
    if (not isinstance(context, list) or not 1 <= len(context) <= 100
            or any(not isinstance(v, str) or not v.strip() or len(v) > 256
                   or any(ord(c) < 32 for c in v) for v in context)):
        raise ValueError("knowledge connectors require an explicit owner scope")
    context = sorted({v.strip() for v in context})
    if not 1 <= max_pages <= 100:
        raise ValueError("max_pages must be in 1-100")
    encoded = urllib.parse.quote(resource, safe="/" if provider == "github" else "")
    host, route = PROVIDERS[provider]
    if provider in ("zendesk", "servicenow", "salesforce"):
        import re

        if not re.fullmatch(r"[a-zA-Z0-9-]{1,63}", resource):
            raise ValueError("provider resource must be a tenant subdomain")
    base = host.format(resource=encoded)
    url = base+route.format(resource=encoded)
    key = hashlib.sha256((provider+"\0"+resource+"\0"+json.dumps(context)).encode()).hexdigest()
    statefile = os.path.join(paths.memory_dir(root), "connectors", "knowledge-"+key+".json")
    paths.enforce_boundary(root, statefile)
    opener = urllib.request.build_opener(NoRedirect)
    transport_cursor = [None]
    def request(address):
        headers = {"Authorization": "Bearer "+token, "Accept": "application/json"}
        if provider == "greenhouse":
            import base64

            headers["Authorization"] = "Basic "+base64.b64encode((token+":").encode()).decode("ascii")
        if provider == "notion":
            headers["Notion-Version"] = "2025-09-03"
        req = urllib.request.Request(address, headers=headers)
        if provider == "linear":
            cursor = urllib.parse.parse_qs(urllib.parse.urlsplit(address).query).get("cursor", [None])[0]
            body = {"query": "query($team:String!,$after:String){issues(first:100,after:$after,"
                    "filter:{team:{id:{eq:$team}}}){nodes{title description}pageInfo{hasNextPage endCursor}}}",
                    "variables": {"team": resource, "after": cursor}}
            req = urllib.request.Request(base+"/graphql", data=json.dumps(body).encode(),
                                         headers={**headers, "Content-Type": "application/json"})
        with opener.open(req, timeout=30) as response:
            data = response.read(8*1024*1024+1)
            if len(data) > 8*1024*1024:
                raise ValueError("provider response exceeds 8 MiB")
            text = data.decode("utf-8")
            if provider == "github":
                import re

                link = response.headers.get("Link", "")
                transport_cursor[0] = next(iter(re.findall(r'<([^>]+)>;\s*rel="next"', link)), None)
            if provider == "servicenow":
                import re

                transport_cursor[0] = next(iter(re.findall(
                    r'<([^>]+)>;\s*rel="next"', response.headers.get("Link", ""))), None)
        if provider == "drive":
            item = json.loads(text)
            mime = item.get("mimeType", "")
            native_types = {"application/vnd.google-apps.document": "text/plain",
                            "application/vnd.google-apps.presentation": "text/plain",
                            "application/vnd.google-apps.spreadsheet": "text/csv"}
            endpoint = base+"/drive/v3/files/"+encoded
            if mime in native_types:
                endpoint += "/export?"+urllib.parse.urlencode({"mimeType": native_types[mime]})
            elif mime.startswith("application/vnd.google-apps."):
                raise ValueError("Drive item is not an exportable text document")
            else:
                endpoint += "?alt=media"
            with opener.open(urllib.request.Request(endpoint, headers=headers), timeout=30) as response:
                data = response.read(8*1024*1024+1)
                if len(data) > 8*1024*1024:
                    raise ValueError("provider content exceeds 8 MiB")
                text = data.decode("utf-8")
        if provider == "onedrive":
            item = json.loads(text)
            download = item.get("@microsoft.graph.downloadUrl", "")
            address = urllib.parse.urlsplit(download)
            domain = address.hostname or ""
            if (address.scheme != "https" or address.username or address.password
                    or not any(domain.endswith("."+suffix) or domain == suffix
                               for suffix in ("sharepoint.com", "onedrive.com", "1drv.com"))):
                raise ValueError("invalid Microsoft preauthenticated download origin")
            # The signed download URL needs no OAuth header; never forward the provider token.
            with opener.open(urllib.request.Request(download), timeout=30) as response:
                data = response.read(8*1024*1024+1)
                if len(data) > 8*1024*1024:
                    raise ValueError("provider content exceeds 8 MiB")
                text = data.decode("utf-8")
        return text
    fetch = fetch or request
    written, pages = 0, 0
    with _jsonl.locked(statefile):
        try:
            with open(statefile, encoding="utf-8") as fh:
                state = json.load(fh)
        except FileNotFoundError:
            state = {"cursor": None, "digests": []}
        current = state.get("cursor") or url
        digests = set(state["digests"])
        snapshot_sources = set()
        while current and pages < max_pages:
            if (urllib.parse.urlsplit(current).netloc != urllib.parse.urlsplit(base).netloc
                    or not current.startswith(base+"/")):
                raise ValueError("provider cursor crossed its pinned origin")
            transport_cursor[0] = None
            raw = fetch(current)
            # Downloaded documents are content, even when their text is valid JSON.
            payload = (raw if provider in ("drive", "onedrive") else
                       json.loads(raw) if raw.lstrip().startswith(("{", "[")) else raw)
            if isinstance(payload, dict) and (payload.get("ok") is False or "error" in payload
                                              or payload.get("errors")):
                raise ValueError("provider rejected read request")
            text = _gmail(payload) if provider == "gmail" else _text(payload)
            generation = state.get("generation", "") if replace_snapshot else ""
            if replace_snapshot and hashlib.sha256(text.encode()).hexdigest() == state.get("snapshot_digest"):
                return {"provider": provider, "facts_written": 0, "pages": 1, "pending": False}
            def source_identity(chunk):
                return hashlib.sha256((key+"\0"+generation+"\0"+chunk).encode()).hexdigest() if replace_snapshot else \
                    hashlib.sha256((key+"\0"+chunk).encode()).hexdigest()
            for offset in range(0, len(text), 20000):
                snapshot_sources.add(source_identity(text[offset:offset+20000]))
            digest = hashlib.sha256(text.encode()).hexdigest()
            if (replace_snapshot or digest not in digests) and text.strip():
                # Preserve source snapshots; facts inherit their authenticated external origin.
                for offset in range(0, len(text), 20000):
                    chunk = text[offset:offset+20000]
                    source_id = source_identity(chunk)
                    with memory_authority.writer(provider+":"+key, "external"):
                        trace_io.write_new(root, title=provider+" knowledge snapshot", context=chunk,
                            solution="Read-only provider evidence; no task outcome asserted", tags=["knowledge-source"],
                            trace_id=source_id, extra={"scopes": context})
                        for start in range(0, len(chunk), hierarchical.MAX_STATEMENT_CHARS):
                            paragraph = chunk[start:start+hierarchical.MAX_STATEMENT_CHARS]
                            if paragraph.strip():
                                result = additive_extract.extract(root, paragraph, local=True, scopes=context,
                                                                  source_trace_id=source_id)
                                written += sum(r["action"] == "ADD" for r in result["facts"])
                digests.add(digest)
            next_url = transport_cursor[0]
            if isinstance(payload, dict):
                next_url = payload.get("@odata.nextLink") or payload.get("next_page") or next_url
                if provider == "salesforce" and payload.get("nextRecordsUrl"):
                    next_url = urllib.parse.urljoin(base, payload["nextRecordsUrl"])
                if provider == "linear":
                    page = payload.get("data", {}).get("issues", {}).get("pageInfo", {})
                    if page.get("hasNextPage"):
                        if not isinstance(page.get("endCursor"), str) or not page["endCursor"]:
                            raise ValueError("Linear response omitted its next-page cursor")
                        next_url = url+"?"+urllib.parse.urlencode({"cursor": page["endCursor"]})
                if provider == "jira" and payload.get("nextPageToken"):
                    next_url = url+"&"+urllib.parse.urlencode({"nextPageToken": payload["nextPageToken"]})
                if provider == "confluence" and payload.get("_links", {}).get("next"):
                    next_url = urllib.parse.urljoin(current, payload["_links"]["next"])
                cursor = payload.get("response_metadata", {}).get("next_cursor") or payload.get("next_cursor")
                if cursor:
                    parameter = "cursor" if provider == "slack" else "start_cursor"
                    next_url = url+"&"+urllib.parse.urlencode({parameter: cursor})
            current, pages = next_url, pages+1
        # No watermark is advanced when a page or extraction failed.
        if replace_snapshot:
            if current or provider not in ("drive", "onedrive"):
                raise ValueError("snapshot replacement requires a complete single-document read")
            for source_id in set(state.get("sources", []))-snapshot_sources:
                hierarchical.retire_source(root, source_id, keep=set())
        _jsonl.write_json(statefile, {"cursor": current, "digests": sorted(digests), "provider": provider,
                                    "snapshot_digest": digest if replace_snapshot else None,
                                    "generation": hashlib.sha256(
                                        (state.get("generation", "")+digest).encode()).hexdigest()
                                                  if replace_snapshot else "",
                                    "sources": sorted(snapshot_sources)})
    return {"provider": provider, "facts_written": written, "pages": pages, "pending": bool(current)}
