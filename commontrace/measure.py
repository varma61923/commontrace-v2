"""Measure whether memory from ANY store causes better outcomes.

Every agent memory system retrieves something and injects it; none of the
widely used ones can say whether what it injected helped. This module lets
an application keep the memory store it already has and add that answer:
wrap the store's retrieval call, report whether each task succeeded, and
`commontrace experiment` reports the causal effect of each memory -- with
the same randomization, the same validity audit and the same statistics it
applies to this store's own lessons.

    from commontrace.measure import CausalMemory

    memory = CausalMemory(my_store.search)          # any callable
    items = memory.recall("stripe webhook retried twice", occasion_id=task.id)
    ...                                             # run the task with `items`
    memory.record_outcome(task.id, succeeded=task.passed)

Then `commontrace experiment` in the same store.

WHAT IT DOES AND DOES NOT CHANGE
--------------------------------
It never alters what the wrapped store returns except to withhold a small,
random, per-memory fraction of eligible items on each occasion (the store's
configured holdout rate, `commontrace experiment --configure`). It writes
nothing to the wrapped store and reads nothing else from it.

All assignment and logging goes through commontrace/holdout_io.py, not a
second implementation of it. That is the point: an assignment written here
is indistinguishable from one written by `commontrace query` or the MCP
server, so the integrity checks, the salt scoping and the analysis all
apply without knowing where the memory came from.

WHY IDENTITY IS NOT GUESSED
---------------------------
Each item needs a stable id -- the arm is a hash of (id, occasion, salt).
If an item has no `id`/`key` attribute or key, this raises rather than
falling back to `str(item)`. For most objects that string contains a memory
address, which changes every process, so the same memory would be assigned
to a fresh random arm each run while appearing to be many different
memories. That failure produces no error and a meaningless result, so it is
refused up front. Pass `key=` to say how your store identifies an item.

WHY CONTENT IS HASHED
---------------------
Stores that update a memory in place keep its id while changing its text.
The id alone would then pool occasions treated with different content into
one arm and report an effect for a treatment that no longer exists. So each
item's text is hashed into the assignment's `revision`, which the existing
revision check reads. If no text can be found, the revision is recorded as
unknown -- reported as unchecked, never guessed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from typing import Any

from commontrace import holdout_io, paths

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


class CausalMemory:
    """Wrap any retrieval callable so its memories can be measured causally.

    `retrieve` is called as `retrieve(query, **kwargs)` and must return an
    iterable of items in rank order. `key(item)` gives each item a stable
    id; `text(item)` gives the content hashed into its revision. `pinned`
    names ids that are always delivered and never randomized -- memories
    already known to help, for which withholding would cost outcomes and
    add no information -- and no assignment is recorded for them at all.
    """

    def __init__(
        self,
        retrieve: Callable[..., Iterable[Any]],
        *,
        root: str | None = None,
        key: Callable[[Any], str] = default_key,
        text: Callable[[Any], str | None] = default_text,
        pinned: Iterable[str] = (),
        scorer: str = "external",
    ) -> None:
        if not callable(retrieve):
            raise TypeError("retrieve must be callable")
        self._retrieve = retrieve
        self._root = paths.resolve_root(root)
        self._key = key
        self._text = text
        self._pinned = frozenset(str(p) for p in pinned)
        self._scorer = scorer

    @property
    def root(self) -> str:
        return self._root

    def recall(self, query: Any, *, occasion_id: str, **kwargs: Any) -> list[Any]:
        """Retrieve for `query` on `occasion_id`, withholding the randomized
        fraction, and return what the task should actually receive."""
        if not isinstance(occasion_id, str) or not occasion_id.strip():
            raise ValueError("occasion_id must be a non-empty string")
        items = list(self._retrieve(query, **kwargs))

        # One row per memory per occasion. A store can return the same memory
        # twice (two chunks of one record, or a duplicate index entry);
        # logging both would count one treatment decision as two
        # observations. Assignment is a hash of the id, so both copies are
        # in the same arm anyway -- the second is simply not logged again.
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
            )
        return [item for item_id, item in keyed if item_id not in withheld]

    def record_outcome(self, occasion_id: str, *, succeeded: bool) -> bool:
        """Report whether the task on `occasion_id` succeeded. Idempotent for
        a repeated identical report; raises holdout_io.ConflictingOutcome if
        a different answer is already on record."""
        return holdout_io.record_outcome(self._root, occasion_id, succeeded)
