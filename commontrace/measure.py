"""Measure whether memory from ANY store causes better outcomes."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from commontrace import evidence, harm, holdout_io, paths, retrieval_io

DEFAULT_CHECK_EVERY = 25

_ID_FIELDS = ("id", "key")
_TEXT_FIELDS = ("memory", "text", "content", "value")


def _field(item: Any, names: Iterable[str]) -> Any:
    for name in names:
        if isinstance(item, dict):
            if name in item:
                return item[name]
        elif hasattr(item, name):
            return getattr(item, name)
    return None


def default_key(item: Any) -> str:
    value = _field(item, _ID_FIELDS)
    if value is None or str(value).strip() == "":
        raise TypeError(
            f"cannot find a stable id on {type(item).__name__} (looked for "
            f"{', '.join(_ID_FIELDS)}); pass key= to CausalMemory. Falling back "
            "to str(item) is refused because it usually changes between processes."
        )
    return str(value)


def default_text(item: Any) -> str | None:
    value = _field(item, _TEXT_FIELDS)
    if value is None:
        return None
    return value if isinstance(value, str) else repr(value)


def content_revision(text: str | None) -> str | None:
    if text is None:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class HarmWatch:
    def __init__(self, root: str, on_harm: str | None = None,
                 check_every: int = DEFAULT_CHECK_EVERY) -> None:
        if on_harm is not None and on_harm not in harm.POLICIES:
            raise ValueError(f"on_harm must be one of {', '.join(harm.POLICIES)} (or None)")
        if not isinstance(check_every, int) or isinstance(check_every, bool) or check_every < 1:
            raise ValueError("check_every must be a whole number of at least 1")
        self._root, self._on_harm, self._every = root, on_harm, check_every
        self._calls = 0
        self._harmful: dict[str, dict] = {}
        self._lock = threading.Lock()

    def current(self) -> dict[str, dict]:
        with self._lock:
            due = self._calls % self._every == 0
            self._calls += 1
            if due:
                policy = self._on_harm or retrieval_io.read_harm_policy(self._root)
                self._harmful = evidence.withdrawn(self._root, policy)
            return self._harmful


@dataclass(frozen=True)
class Recall:
    items: list
    withdrawn: dict = field(default_factory=dict)
    quarantined: dict = field(default_factory=dict)


class CausalMemory:
    """Wrap any retrieval callable so its memories can be measured causally."""

    def __init__(
        self,
        retrieve: Callable[..., Iterable[Any]],
        *,
        root: str | None = None,
        key: Callable[[Any], str] = default_key,
        text: Callable[[Any], str | None] = default_text,
        pinned: Iterable[str] = (),
        scorer: str = "external",
        on_harm: str | None = None,
        check_every: int = DEFAULT_CHECK_EVERY,
        harm_watch: HarmWatch | None = None,
        durable: bool = True,
        screen: bool = False,
    ) -> None:
        if not callable(retrieve):
            raise TypeError("retrieve must be callable")
        self._watch = harm_watch or HarmWatch(paths.resolve_root(root), on_harm, check_every)
        self._durable = durable
        self._retrieve = retrieve
        self._root = paths.resolve_root(root)
        self._key = key
        self._text = text
        self._pinned = frozenset(str(p) for p in pinned)
        self._scorer = scorer
        self._screen = screen

    @property
    def root(self) -> str:
        return self._root

    def recall(self, query: Any, *, occasion_id: str, **kwargs: Any) -> list[Any]:
        return self.recall_detailed(query, occasion_id=occasion_id, **kwargs).items

    def _withdrawn(self) -> dict[str, dict]:
        return self._watch.current()

    def recall_detailed(self, query: Any, *, occasion_id: str, **kwargs: Any) -> Recall:
        """`recall`, plus which memories were withdrawn for measured harm."""
        if not isinstance(occasion_id, str) or not occasion_id.strip():
            raise ValueError("occasion_id must be a non-empty string")
        items = list(self._retrieve(query, **kwargs))

        harmful = self._withdrawn()
        removed: dict[str, dict] = {}
        if harmful:
            kept = []
            for item in items:
                item_id = str(self._key(item))
                if item_id in harmful and item_id not in self._pinned:
                    removed[item_id] = harmful[item_id]
                else:
                    kept.append(item)
            items = kept

        quarantined: dict[str, str] = {}
        if self._screen:
            from commontrace import injection_guard

            kept = []
            for item in items:
                labels = injection_guard.injection_labels({"text": self._text(item)})
                if labels:
                    quarantined[str(self._key(item))] = "injection screen: " + ", ".join(labels)
                else:
                    kept.append(item)
            items = kept

        ids: list[str] = []
        revisions: dict[str, str | None] = {}
        keyed: list[tuple[str, Any]] = []
        for item in items:
            item_id = str(self._key(item))
            keyed.append((item_id, item))
            if item_id in self._pinned or item_id in revisions:
                continue
            ids.append(item_id)
            revisions[item_id] = content_revision(self._text(item))

        config = holdout_io.load_config(self._root)
        withheld: set[str] = set()
        if ids and config.running:
            withheld = holdout_io.assign_and_log(
                self._root,
                ids,
                occasion_id=occasion_id,
                rate=config.rate,
                salt=config.salt,
                scorer=self._scorer,
                revisions=revisions,
                durable=self._durable,
            )
        return Recall(
            items=[item for item_id, item in keyed if item_id not in withheld], withdrawn=removed,
            quarantined=quarantined,
        )

    def record_outcome(self, occasion_id: str, *, succeeded: bool) -> bool:
        return holdout_io.record_outcome(self._root, occasion_id, succeeded, self._durable)
