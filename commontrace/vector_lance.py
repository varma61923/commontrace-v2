"""Optional scoped LanceDB cosine index; returned IDs still need canonical admission."""
from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterable, Sequence

from commontrace import _jsonl, paths
from commontrace.async_workers import STORE_WORKERS
from commontrace.vector_store import MAX_BATCH, VectorHit, VectorRecord, _generation, _keys, _limit, _vector


def _literal(value: str) -> str:
    return "'"+value.replace("'", "''")+"'"


class LanceVectorIndex:
    def __init__(self, root: str, *, tenant: str, namespace: str, model: str, dimension: int):
        from commontrace.vector_store import _Scope

        scope = _Scope(tenant, namespace, model, dimension)
        self.tenant, self.namespace, self.model, self.dimension = scope.sql()
        try:
            import lancedb
            import pyarrow as pa
        except ImportError:
            raise RuntimeError("Lance indexing requires commontrace[vector-lance]") from None
        directory = os.path.join(paths.memory_dir(root), "lance")
        paths.enforce_boundary(root, directory)
        paths.safe_prepare_output_path(os.path.join(directory, "index"))
        self._lock = threading.Lock()
        self._closed = False
        name = "vectors_"+hashlib.sha256(repr(scope.sql()).encode()).hexdigest()
        self._identity = os.path.join(directory, name+".json")
        self._db = lancedb.connect(directory)
        schema = pa.schema([pa.field("key", pa.string()), pa.field("vector", pa.list_(pa.float32(), dimension)),
                            pa.field("generation", pa.string()), pa.field("checksum", pa.string())])
        self._table = self._db.create_table(name, schema=schema, exist_ok=True)

    async def _run(self, operation):
        def execute():
            with self._lock:
                if self._closed:
                    raise RuntimeError("Lance index is closed")
                return operation()
        return await STORE_WORKERS.run(execute)

    async def bind_source(self, identity: str) -> None:
        def execute():
            import json

            with _jsonl.locked(self._identity):
                if os.path.isfile(self._identity):
                    with open(self._identity, encoding="utf-8") as fh:
                        prior = json.load(fh)
                    if prior["source"] != identity:
                        raise ValueError("index is already bound to a different canonical source")
                else:
                    _jsonl.write_json(self._identity, {"source": identity})
        await self._run(execute)

    async def upsert(self, records: Iterable[VectorRecord]) -> int:
        rows = []
        for record in records:
            if len(rows) >= MAX_BATCH:
                raise ValueError("vector batch exceeds the bounded ingestion limit")
            rows.append({"key": _keys([record.key])[0], "vector": list(_vector(record.vector, self.dimension)),
                         "generation": _generation(record.generation), "checksum": record.checksum})
        def execute():
            if rows:
                self._table.merge_insert("key").when_matched_update_all().when_not_matched_insert_all().execute(rows)
            return len(rows)
        return await self._run(execute)

    async def delete(self, keys: Iterable[str]) -> int:
        ids = _keys(keys)
        def execute():
            if not ids:
                return 0
            where = "key IN ("+",".join(_literal(i) for i in ids)+")"
            count = self._table.count_rows(where)
            self._table.delete(where)
            return count
        return await self._run(execute)

    async def prune(self, generation: str) -> int:
        where = "generation != "+_literal(_generation(generation))
        def execute():
            count = self._table.count_rows(where)
            self._table.delete(where)
            return count
        return await self._run(execute)

    async def search(self, vector: Sequence[float], *, top_k: int = 10, generation: str | None = None,
                     allowed_ids: Sequence[str] | None = None) -> list[VectorHit]:
        _limit(top_k)
        query = list(_vector(vector, self.dimension))
        ids = _keys(allowed_ids) if allowed_ids is not None else None
        def execute():
            if top_k == 0 or ids == []:
                return []
            clauses = []
            if generation is not None:
                clauses.append("generation = "+_literal(_generation(generation)))
            if ids is not None:
                clauses.append("key IN ("+",".join(_literal(i) for i in ids)+")")
            search = self._table.search(query).distance_type("cosine")
            if clauses:
                search = search.where(" AND ".join(clauses), prefilter=True)
            return [VectorHit(r["key"], 1-float(r["_distance"])) for r in search.limit(top_k).to_list()]
        return await self._run(execute)

    async def close(self) -> None:
        with self._lock:
            self._closed = True
