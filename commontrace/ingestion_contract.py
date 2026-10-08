"""Batch/timestamp/alias guards shared by local and HTTP ADD-only admission."""
from __future__ import annotations

import re
from datetime import datetime

from commontrace import hierarchical, ontology


def batch(root: str, items: list[dict], *, context: list[str] | None = None) -> dict:
    if not isinstance(items, list) or len(items) > 200:
        raise ValueError("batch must contain at most 200 objects")
    aliases = ontology.load(root).aliases()
    prepared = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("statement"), str):
            raise ValueError("each batch item needs a statement")
        row = dict(item)
        for field in ("valid_from", "valid_until", "expires_at"):
            stamp = row.get(field)
            if stamp is not None:
                if not isinstance(stamp, str):
                    raise ValueError("timestamps must be ISO 8601 strings")
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("batch timestamps require an explicit timezone")
        text = row["statement"]
        for canonical, variants in aliases.items():
            for alias in sorted(variants, key=len, reverse=True):
                text = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", lambda _match: canonical,
                              text, flags=re.IGNORECASE)
        row["statement"] = text
        if context is not None:
            row["scopes"] = context
        prepared.append(row)
    results = hierarchical.append_facts(root, prepared)
    return {"facts": [{"action": action, **fact.to_dict()} for fact, action in results], "add_only": True}
