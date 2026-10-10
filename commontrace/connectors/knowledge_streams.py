"""Bounded mailbox/change-feed/repository reads and authenticated GitHub push sync."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import urllib.parse
import urllib.request

from commontrace import _jsonl, additive_extract, hierarchical, memory_authority, paths, trace_io
from commontrace.connectors import knowledge
from commontrace.secrets_provider import env_secret

STREAMS = ("gmail-mailbox", "drive-changes", "github-repo")
MAX_ITEMS = 200


def _request(origin: str, token: str, fetch=None):
    opener = urllib.request.build_opener(knowledge.NoRedirect)
    def get(url):
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(origin).netloc or not url.startswith(origin+"/"):
            raise ValueError("stream cursor crossed its pinned origin")
        if fetch is not None:
            raw = fetch(url)
        else:
            from commontrace import offline

            offline.check_url(url, "a knowledge stream")
            request = urllib.request.Request(url, headers={"Authorization": "Bearer "+token,
                                                          "Accept": "application/json"})
            with opener.open(request, timeout=30) as response:
                raw = response.read(8*1024*1024+1).decode("utf-8")
        if not isinstance(raw, str) or len(raw.encode()) > 8*1024*1024:
            raise ValueError("stream response exceeds 8 MiB")
        value = json.loads(raw)
        if not isinstance(value, dict) or "error" in value or value.get("errors"):
            raise ValueError("provider rejected stream read")
        return value
    return get


def _document(root: str, provider: str, source: str, text: str, context: list[str]) -> int:
    key = hashlib.sha256((provider+source+repr(context)).encode()).hexdigest()
    state = os.path.join(paths.memory_dir(root), "connectors", "document-"+key+".json")
    paths.enforce_boundary(root, state)
    paths.safe_prepare_output_path(state)
    with _jsonl.locked(state), memory_authority.restricted_writer(provider+":"+key, "external"):
        previous = {}
        if os.path.exists(state):
            with open(state, encoding="utf-8") as stream:
                previous = json.load(stream)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if previous.get("digest") == digest:
            return 0
        source_ids, written = [], 0
        for offset in range(0, len(text), 20000):
            chunk = text[offset:offset+20000]
            source_id = hashlib.sha256((key+previous.get("generation", "")+digest+str(offset)).encode()).hexdigest()
            source_ids.append(source_id)
            trace_io.write_new(root, title=provider+" source snapshot", context=chunk,
                solution="Read-only external source; no outcome or action authority asserted.",
                tags=["knowledge-source"], trace_id=source_id, extra={"scopes": context})
            for start in range(0, len(chunk), hierarchical.MAX_STATEMENT_CHARS):
                result = additive_extract.extract(root, chunk[start:start+hierarchical.MAX_STATEMENT_CHARS],
                                                 local=True, scopes=context, source_trace_id=source_id)
                written += sum(row["action"] == "ADD" for row in result["facts"])
        # Retire only this connection's former evidence after all new writes succeed.
        for source_id in set(previous.get("sources", []))-set(source_ids):
            hierarchical.retire_source(root, source_id, keep=set())
        _jsonl.write_json(state, {"digest": digest, "generation": hashlib.sha256(
            (previous.get("generation", "")+digest).encode()).hexdigest(), "sources": source_ids, "scopes": context})
        return written


def sync(root: str, provider: str, resource: str, *, token: str, context: list[str], account: str,
         max_pages: int = 10, fetch=None, ref: str = "HEAD") -> dict:
    if provider not in STREAMS or not token or not account or len(account) > 128:
        raise ValueError("stream requires provider, OAuth token and an explicit connection/account name")
    if not isinstance(context, list) or not context or len(context) > 100 or any(
            not isinstance(s, str) or not s.strip() or len(s) > 256 or any(ord(c) < 32 for c in s) for s in context):
        raise ValueError("stream requires owner scopes")
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 20:
        raise ValueError("stream page limit must be in 1..20")
    context = sorted(set(context))
    key = hashlib.sha256((provider+resource+account+repr(context)).encode()).hexdigest()
    state = os.path.join(paths.memory_dir(root), "connectors", "stream-"+key+".json")
    paths.enforce_boundary(root, state)
    paths.safe_prepare_output_path(state)
    with _jsonl.locked(state):
        before = {}
        if os.path.exists(state):
            with open(state, encoding="utf-8") as stream:
                before = json.load(stream)
        written = count = 0
        def document(identity, text):
            nonlocal count, written
            count += 1
            if count > MAX_ITEMS:
                raise ValueError("stream item budget exceeded; reduce the selection")
            written += _document(root, provider, account+":"+identity, text, context)
        if provider == "github-repo":
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", resource):
                raise ValueError("GitHub resource must be owner/repository")
            get = _request("https://api.github.com", token, fetch)
            base = "https://api.github.com/repos/"+resource
            tree = get(base+"/git/trees/"+urllib.parse.quote(ref, safe="")+"?recursive=1")
            if tree.get("truncated"):
                raise ValueError("GitHub tree was truncated; refusing an incomplete repository snapshot")
            active = []
            for entry in tree.get("tree", []):
                filename = entry.get("path", "")
                if entry.get("type") != "blob" or os.path.splitext(filename)[1] not in (
                        ".md", ".txt", ".rst", ".py", ".ts", ".tsx", ".json", ".yaml", ".yml"):
                    continue
                if entry.get("size", 0) > 1024*1024 or not re.fullmatch(r"[0-9a-f]{40,64}", entry.get("sha", "")):
                    raise ValueError("GitHub blob exceeds limits or has an invalid object ID")
                blob = get(base+"/git/blobs/"+entry["sha"])
                if blob.get("encoding") != "base64":
                    raise ValueError("unsupported GitHub blob encoding")
                content = base64.b64decode("".join(blob.get("content", "").split()), validate=True)
                if len(content) > 1024*1024:
                    raise ValueError("GitHub blob exceeds decoded size limit")
                identity = resource+":"+filename
                document(identity, content.decode("utf-8"))
                active.append(identity)
            for identity in set(before.get("files", []))-set(active):
                document(identity, "")
            after = {"ref": tree.get("sha"), "files": active}
        else:
            origin = "https://gmail.googleapis.com" if provider == "gmail-mailbox" else "https://www.googleapis.com"
            get = _request(origin, token, fetch)
            cursor = before.get("cursor")
            if before.get("current"):
                current = before["current"]
                bootstrap = bool(before.get("bootstrap"))
            elif provider == "drive-changes" and not cursor:
                # Capture the feed watermark before bootstrapping so concurrent
                # file updates are observed by the subsequent change-feed pass.
                cursor = get(origin+"/drive/v3/changes/startPageToken")["startPageToken"]
                current = origin+"/drive/v3/files?"+urllib.parse.urlencode(
                    {"q": "trashed=false", "pageSize": 100, "fields": "nextPageToken,files(id)"})
                bootstrap = True
            elif provider == "drive-changes":
                current = origin+"/drive/v3/changes?"+urllib.parse.urlencode(
                    {"pageToken": cursor, "pageSize": 100,
                     "fields": "nextPageToken,newStartPageToken,changes(fileId,removed)"})
                bootstrap = False
            else:
                current = origin+"/gmail/v1/users/me/messages?"+urllib.parse.urlencode(
                    {"q": resource, "maxResults": 100, **({"pageToken": cursor} if cursor else {})})
                bootstrap = False
            page_number = 0
            while current and page_number < max_pages and count < MAX_ITEMS:
                page = get(current)
                entries = (page.get("messages", []) if provider == "gmail-mailbox" else
                           page.get("files", []) if bootstrap else page.get("changes", []))
                for entry in entries:
                    identity = entry.get("fileId", entry.get("id"))
                    if not isinstance(identity, str) or not identity:
                        raise ValueError("provider omitted an item ID")
                    if entry.get("removed"):
                        if provider == "drive-changes":
                            _retire_drive(root, identity, context)
                            count += 1
                        else:
                            document(identity, "")
                    elif provider == "gmail-mailbox":
                        value = get(origin+"/gmail/v1/users/me/messages/"
                                    +urllib.parse.quote(identity, safe="")+"?format=full")
                        document(identity, knowledge._gmail(value))
                    else:
                        # Reuse the pinned native Drive export/media reader. Its
                        # per-file checkpoint survives a later page failure.
                        result = knowledge.sync(root, "drive", identity, token=token, context=context, fetch=fetch,
                                                replace_snapshot=True)
                        written += result["facts_written"]
                        count += 1
                        if count > MAX_ITEMS:
                            raise ValueError("stream item budget exceeded")
                next_token = page.get("nextPageToken")
                if next_token:
                    params = urllib.parse.parse_qs(urllib.parse.urlsplit(current).query)
                    params["pageToken"] = [next_token]
                    current = current.split("?", 1)[0]+"?"+urllib.parse.urlencode(params, doseq=True)
                else:
                    current = ""
                    cursor = cursor if bootstrap else page.get("newStartPageToken", cursor)
                page_number += 1
                # A page watermark is durable only after all its documents succeed.
                # Partial bootstraps retain both the original feed token and page URL.
                _jsonl.write_json(state, {"cursor": cursor, "current": current,
                    "bootstrap": bootstrap, "provider": provider, "scopes": context, "account": account})
            after = {"cursor": cursor if provider == "drive-changes" else None,
                     "current": current, "bootstrap": bootstrap}
        _jsonl.write_json(state, {**after, "provider": provider, "scopes": context, "account": account})
    return {"provider": provider, "facts_written": written, "items": count,
            "complete": not bool(after.get("current"))}


def _retire_drive(root: str, resource: str, context: list[str]) -> None:
    key = hashlib.sha256(("drive\0"+resource+"\0"+json.dumps(context)).encode()).hexdigest()
    path = os.path.join(paths.memory_dir(root), "connectors", "knowledge-"+key+".json")
    paths.enforce_boundary(root, path)
    if not os.path.exists(path):
        return
    with _jsonl.locked(path):
        with open(path, encoding="utf-8") as stream:
            before = json.load(stream)
        for source_id in before.get("sources", []):
            hierarchical.retire_source(root, source_id, keep=set())
        _jsonl.write_json(path, {"cursor": None, "digests": [], "provider": "drive", "sources": []})


def configure_github_webhook(root: str, repository: str, *, token_env: str, secret_env: str,
                             context: list[str], account: str) -> None:
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name) for name in (token_env, secret_env)):
        raise ValueError("webhook credentials must refer to server environment variable names")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not context or not account:
        raise ValueError("webhook requires repository, owner scopes and account")
    path = os.path.join(paths.memory_dir(root), "connectors", "github-webhook.json")
    paths.enforce_boundary(root, path)
    _jsonl.write_json(paths.safe_prepare_output_path(path), {"repository": repository, "token_env": token_env,
                     "secret_env": secret_env, "context": context, "account": account})
    os.chmod(path, 0o600)


def github_push(root: str, body: bytes, signature: str, delivery: str, event: str, *, fetch=None) -> dict:
    path = os.path.join(paths.memory_dir(root), "connectors", "github-webhook.json")
    paths.enforce_boundary(root, path)
    with open(path, encoding="utf-8") as stream:
        config = json.load(stream)
    secret = env_secret(config["secret_env"])
    if len(secret) < 32 or len(body) > 1024*1024 or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", delivery):
        raise PermissionError("invalid webhook credential, delivery or body")
    expected = "sha256="+hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise PermissionError("GitHub webhook signature mismatch")
    if event != "push":
        return {"ignored": True}
    payload = json.loads(body)
    if payload.get("repository", {}).get("full_name") != config["repository"]:
        raise PermissionError("GitHub webhook repository mismatch")
    commit = payload.get("after", "")
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit) or set(commit) == {"0"}:
        raise ValueError("GitHub push must identify a committed repository snapshot")
    receipt_key = hashlib.sha256(config["repository"].encode()+b"\0"+body).hexdigest()
    receipt = os.path.join(paths.memory_dir(root), "connectors", "delivery-"+receipt_key+".json")
    paths.enforce_boundary(root, receipt)
    paths.safe_prepare_output_path(receipt)
    with _jsonl.locked(path+"-push"):
        if os.path.exists(receipt):
            return {"replayed": True}
        branch = payload.get("ref", "")
        if not isinstance(branch, str) or not branch.startswith("refs/heads/"):
            raise ValueError("GitHub push must identify its branch")
        token = env_secret(config["token_env"])
        get = _request("https://api.github.com", token, fetch)
        head = get("https://api.github.com/repos/"+config["repository"]+"/git/ref/"+
                   urllib.parse.quote(branch.removeprefix("refs/"), safe="/"))
        if head.get("object", {}).get("sha") != commit:
            return {"ignored": True, "reason": "superseded push"}
        result = sync(root, "github-repo", config["repository"], token=env_secret(config["token_env"]),
                      context=config["context"], account=config["account"], ref=commit, fetch=fetch)
        _jsonl.write_json(receipt, {"commit": commit, "delivery": delivery})
        return result
