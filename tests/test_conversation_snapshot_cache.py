"""Bounded cache accounting and adversarial snapshot acquisition races."""
from commontrace.conversation import Options, Store, recall, search


def test_one_snapshot_uses_constant_version_checks(tmp_path):
    with Store(str(tmp_path), "user") as store:
        store.add("s", [{"role": "user", "text": "Orion launch is ready."}])
        queries = []
        store.db.set_trace_callback(queries.append)
        with store.read_snapshot():
            for _ in range(200):
                assert store.turns([1])[1].text == "Orion launch is ready."
                store.read_stamp()
            with store.read_snapshot():
                assert store.turns([1])
        assert sum(q.lower().startswith("pragma data_version") for q in queries) == 1
        assert store._snapshot_stamp is None


def test_external_delete_is_visible_after_existing_snapshot_finishes(tmp_path):
    with Store(str(tmp_path), "user") as reader, Store(str(tmp_path), "user") as writer:
        writer.add("s", [{"role": "user", "text": "Orion secret launch schedule."}])
        with reader.read_snapshot():
            assert reader.turns([1])
            writer.delete_session("s")
            assert reader.turns([1])  # the deliberately pinned older read view
        with reader.read_snapshot():
            assert reader.turns([1]) == {}


class _CallbackCursor:
    def __init__(self, cursor, callback):
        self.cursor, self.callback = cursor, callback

    def fetchone(self):
        value = self.cursor.fetchone()
        self.callback()
        return value


class _RacingConnection:
    def __init__(self, connection, writer, phase):
        self.connection, self.writer, self.phase = connection, writer, phase
        self.fired = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def delete(self):
        self.fired = True
        self.writer.delete_session("s")

    def execute(self, query, *args):
        if not self.fired and self.phase == "before-pin" and query == "PRAGMA data_version":
            self.delete()
        cursor = self.connection.execute(query, *args)
        if not self.fired and self.phase == "after-pin" and query == "PRAGMA data_version":
            return _CallbackCursor(cursor, self.delete)
        return cursor



def _race(tmp_path, phase):
    opts = Options(embedder="none", rerank=None, instructions=0, profile_facts=0)
    with Store(str(tmp_path), "user") as reader, Store(str(tmp_path), "user") as writer:
        writer.add("s", [{"role": "user", "text": "Orion launch schedule is private."}])
        assert "private" in recall(reader, "Orion launch schedule", options=opts).context
        reader.db = _RacingConnection(reader.db, writer, phase)
        raced = recall(reader, "Orion launch schedule", options=opts)
        assert reader.db.fired
        if phase == "before-pin":
            assert not raced.context
        else:
            assert "private" in raced.context
        assert not recall(reader, "Orion launch schedule", options=opts).context


def test_commit_between_version_check_and_snapshot_pin_cannot_hit_old_cache(tmp_path):
    _race(tmp_path, "before-pin")


def test_commit_after_pin_cannot_publish_old_evidence_under_new_revision(tmp_path):
    _race(tmp_path, "after-pin")


def test_local_writes_and_rollback_invalidate_snapshot_turn_cache(tmp_path):
    with Store(str(tmp_path), "user") as store:
        store.add("s", [{"role": "user", "text": "Orion launch ready."}])
        try:
            with store.read_snapshot():
                assert store.turns([1])
                store.db.execute("DELETE FROM turns WHERE id=1")
                assert store.turns([1]) == {}
                raise ValueError("rollback")
        except ValueError:
            pass
        assert store._snapshot_stamp is None
        assert store.turns([1])


def test_cache_sizes_are_counted_once_and_enforce_metadata_bytes(tmp_path, monkeypatch):
    calls = []
    original = search._recall_size

    def observed(result):
        calls.append(result.question)
        return original(result)

    monkeypatch.setattr(search, "_recall_size", observed)
    monkeypatch.setattr(search, "_RECALL_CACHE_BYTES", 24_000)
    with Store(str(tmp_path), "user") as store:
        store.add("s", [{"role": "user", "text": "Orion launch is ready."}])
        opts = Options(embedder="none", rerank=None, context_strategy="coverage-v2")
        for i in range(20):
            recall(store, f"Orion launch query {i}", options=opts)
            assert sum(entry.size for entry in search._RECALL_CACHE.values()) <= 24_000
        assert len(calls) == 20
        assert all(entry.size == original(entry.result) for entry in search._RECALL_CACHE.values())


def test_auto_model_reconfiguration_cannot_reuse_old_response(tmp_path, monkeypatch):
    from commontrace.conversation import embed

    configured = [None]
    monkeypatch.setattr(embed, "configured", lambda: configured[0])
    monkeypatch.setattr(embed, "available_for", lambda tag: True)
    monkeypatch.setattr(search, "_recall", lambda store, question, **kwargs:
                        search.Recall(question, str(configured[0]), 1, [], [], None))
    with Store(str(tmp_path), "user") as store:
        assert recall(store, "same question").context == "None"
        configured[0] = "minilm"
        assert recall(store, "same question").context == "minilm"


def test_transient_backend_fallback_is_not_sticky_in_response_cache(tmp_path, monkeypatch):
    attempts = []

    def temporary_failure(store, question, **kwargs):
        attempts.append(question)
        return search.Recall(question, "lexical", 2, [], [], None, {"dense_fallback": "temporary failure"})

    monkeypatch.setattr(search, "_recall", temporary_failure)
    with Store(str(tmp_path), "user") as store:
        opts = Options(embedder="none", rerank=None)
        recall(store, "same question", options=opts)
        recall(store, "same question", options=opts)
        assert len(attempts) == 2
