"""UniMem stale generations, scoped reads, leases and reviewable merges."""
import pytest

from unimem import hierarchical as h
from unimem import team_io as io
from unimem import team_isolation as pure


def test_config_and_fetch_are_scoped(tmp_path):
    root = str(tmp_path)
    fact = h.add_fact(root, 'Wrist payload limit is 5 kg', scopes=['tenant:a'])[0]
    io.isolation(root, level='serializable', scopes=['tenant:a'])
    assert io.isolation(root)['level'] == 'serializable'
    read = io.fetch(root, identities=[fact.id], context=['tenant:a'])
    assert read['read_set'][0]['transaction_time'] == pure.transaction_time(h.load_facts(root)[fact.id].to_dict())
    with pytest.raises(PermissionError):
        io.fetch(root, identities=[fact.id], context=['tenant:b'])
    with pytest.raises(PermissionError):
        io.fetch(root, identities=[fact.id], space='bypass', context=['tenant:a'])


def test_lease_fences_reacquisition_and_replay(tmp_path):
    root = str(tmp_path)
    io.isolation(root, level='sequenced')
    first = io.lease(root, work_item='wrist')
    with pytest.raises(pure.StaleGeneration):
        io.lease(root, work_item='wrist')
    io.lease(root, work_item='wrist', release=True, lease_id=first['lease_id'],
                 fencing_token=first['fencing_token'])
    second = io.lease(root, work_item='wrist')
    assert second['fencing_token'] == first['fencing_token'] + 1
    with pytest.raises(pure.StaleGeneration):
        io.lease(root, work_item='wrist', release=True, lease_id=first['lease_id'],
                 fencing_token=first['fencing_token'])


def test_checked_stops_stale_and_snapshot_conflicts(tmp_path):
    root = str(tmp_path)
    fact = h.add_fact(root, 'Wrist payload limit is 5 kg')[0]
    read = io.fetch(root, identities=[fact.id])
    h.update_fact(root, fact.id, confidence=0.9)
    with io.request(read_set=read['read_set']):
        with pytest.raises(pure.StaleGeneration):
            with io._checked(root, h.load_facts(root), {'level':'serializable', 'scopes':[]}, pure.REQUEST.get()):
                pytest.fail('stale must fail before generation')
    fresh = io.fetch(root, identities=[fact.id])
    with io.request(read_set=fresh['read_set']):
        with io._checked(root, h.load_facts(root), {'level':'snapshot', 'scopes':[]}, pure.REQUEST.get()):
            pass


@pytest.mark.parametrize('sqlite', [False, True])
def test_real_transactions_prevent_lost_update(tmp_path, sqlite):
    from unimem import fact_store

    root = str(tmp_path)
    if sqlite:
        fact_store.migrate(root)
    fact = h.add_fact(root, 'Account balance is 10 credits')[0]
    reads = io.fetch(root, identities=[fact.id])['read_set']
    io.isolation(root, level='none')
    # Both writers computed 10 + 10 from the same point read.
    with io.request(read_set=reads):
        h.update_fact(root, fact.id, statement='Account balance is 20 credits')
    with io.request(read_set=reads):
        h.update_fact(root, fact.id, statement='Account balance is 20 credits')
    assert h.load_facts(root)[fact.id].statement == 'Account balance is 20 credits'
    io.isolation(root, level='serializable')
    current = io.fetch(root, identities=[fact.id])['read_set']
    with io.request(read_set=current):
        h.update_fact(root, fact.id, statement='Account balance is 30 credits')
    with io.request(read_set=current):
        with pytest.raises(pure.StaleGeneration):
            h.update_fact(root, fact.id, statement='Account balance is 30 credits')
    assert h.load_facts(root)[fact.id].statement == 'Account balance is 30 credits'
    with pytest.raises(pure.StaleGeneration):
        h.update_fact(root, fact.id, statement='Bypass request')


@pytest.mark.parametrize('sqlite', [False, True])
def test_snapshot_rejects_only_write_conflicts(tmp_path, sqlite):
    from unimem import fact_store

    root = str(tmp_path)
    if sqlite:
        fact_store.migrate(root)
    a = h.add_fact(root, 'The account balance is 10 credits')[0]
    b = h.add_fact(root, 'The account budget is 50 credits')[0]
    io.isolation(root, level='snapshot')
    reads = io.fetch(root, identities=[a.id, b.id])['read_set']
    with io.request(read_set=reads):
        h.update_fact(root, a.id, confidence=0.95)
    with io.request(read_set=reads):
        h.update_fact(root, b.id, confidence=0.95)
    with io.request(read_set=reads):
        with pytest.raises(pure.StaleGeneration):
            h.update_fact(root, a.id, confidence=0.96)


@pytest.mark.parametrize('bad', [None, {}, 'x', [{'id':'a','transaction_time':'tx1:'+'g'*64}],
    [{'id':'a','transaction_time':'tx1:'+'0'*64}]*2, [{'id': 'a', 'transaction_time': 'tx1:'+'0'*64, 'extra':1}],
    [{'id':'\x00','transaction_time':'tx1:'+'0'*64}]])
def test_readset_rejects_malformed_and_ambiguous_versions(bad):
    with pytest.raises(ValueError):
        pure.read_set(bad)


@pytest.mark.parametrize('sqlite', [False, True])
def test_unavailable_readset_cannot_disclose_or_modify_foreign_fact(tmp_path, sqlite):
    from unimem import fact_store

    root = str(tmp_path)
    if sqlite:
        fact_store.migrate(root)
    fact = h.add_fact(root, 'Secret health diagnosis', scopes=['tenant:b'])[0]
    version = pure.transaction_time(h.load_facts(root)[fact.id].to_dict())
    io.isolation(root, level='serializable')
    with io.request(read_set=[{'id':fact.id, 'transaction_time':version}], context=['tenant:a']):
        with pytest.raises(pure.StaleGeneration) as caught:
            h.add_fact(root, 'Safe proposed update', scopes=['tenant:a'])
    assert caught.value.details['superseding_facts'] == []
    with io.request(read_set=[], context=['tenant:a']):
        with pytest.raises(PermissionError):
            h.update_fact(root, fact.id, statement='Overwritten health diagnosis')
    assert h.load_facts(root)[fact.id].statement == 'Secret health diagnosis'


@pytest.mark.parametrize('sqlite', [False, True])
def test_simultaneous_readers_only_one_serializable_writer_commits(tmp_path, sqlite):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from unimem import fact_store

    root = str(tmp_path)
    if sqlite:
        fact_store.migrate(root)
    fact = h.add_fact(root, 'Account balance is 10 credits')[0]
    io.isolation(root, level='serializable')
    barrier = Barrier(2)
    def change(_):
        reads = io.fetch(root, identities=[fact.id])['read_set']
        barrier.wait(timeout=2)
        try:
            with io.request(read_set=reads):
                h.update_fact(root, fact.id, statement='Account balance is 20 credits')
            return 'committed'
        except pure.StaleGeneration:
            return 'stale'
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(change, range(2))) == ['committed', 'stale']


def test_lease_checks_expiry_at_commit_and_rolls_back(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    root = str(tmp_path)
    fact = h.add_fact(root, 'Account balance is 10 credits')[0]
    io.isolation(root, level='sequenced')
    lease = io.lease(root, work_item='balance')
    reads = io.fetch(root, identities=[fact.id])['read_set']
    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) + timedelta(hours=2)
    with io.request(read_set=reads, work_item='balance', lease_id=lease['lease_id'],
                    fencing_token=lease['fencing_token']):
        with pytest.raises(pure.StaleGeneration):
            with h.mutate_facts(root) as facts:
                facts[fact.id].statement = 'Account balance is 20 credits'
                monkeypatch.setattr(io, 'datetime', Future)
    assert h.load_facts(root)[fact.id].statement == 'Account balance is 10 credits'


@pytest.mark.parametrize('sqlite', [False, True])
@pytest.mark.parametrize('writer', ['add_fact', 'add_facts', 'append_facts'])
def test_contradiction_precedes_overeager_near_dedup(tmp_path, monkeypatch, sqlite, writer):
    from unimem import fact_conflicts, fact_store

    root = str(tmp_path)
    if sqlite:
        fact_store.migrate(root)
    older = h.add_fact(root, 'The wrist motor current operating limit is 5 amps')[0]
    io.isolation(root, level='serializable')
    reads = io.fetch(root, identities=[older.id])['read_set']
    monkeypatch.setenv('UNIMEM_FACT_DEDUP', 'near')
    # Team safety takes precedence over the legacy off/supersede preference.
    monkeypatch.setenv('UNIMEM_FACT_CONFLICTS', 'off')
    index_type = fact_store.StatementIndex if sqlite else h._StatementIndex
    def erroneous_near(self, statement, scopes):
        pytest.fail('Contradiction must be checked before a near-dedup call')
    monkeypatch.setattr(index_type, 'near', erroneous_near)
    with io.request(read_set=reads):
        if writer == 'add_fact':
            added, action = h.add_fact(root, 'The wrist motor current operating limit is 7 amps')
        else:
            added, action = getattr(h, writer)(root,
                [{'statement':'The wrist motor current operating limit is 7 amps'}])[0]
    assert action == 'ADD' and added.id != older.id
    pending = fact_conflicts.pending(root)
    assert any(c.older == older.id and c.newer == added.id and c.action == 'flagged' for c in pending)
    assert h.load_facts(root)[older.id].status == 'active'


def test_semantic_merge_keeps_sources_and_requires_review(tmp_path):
    from unimem import fact_conflicts

    root = str(tmp_path)
    older = h.add_fact(root, 'Wrist payload limit is 5 kg', scopes=['tenant:a'])[0]
    io.isolation(root, level='serializable', scopes=['tenant:a'])
    reads = io.fetch(root, identities=[older.id], context=['tenant:a'])['read_set']
    with io.request(read_set=reads, merge=True, context=['tenant:a']):
        merged, action = h.append_facts(root, [{'statement':'Use the lighter grasp for fragile loads',
                                               'scopes':['tenant:a']}])[0]
    assert action == 'ADD'
    assert h.load_facts(root)[older.id].status == 'active'
    assert any(c.older==older.id and c.newer==merged.id and c.kind=='team-merge'
               for c in fact_conflicts.pending(root))


def test_gateway_returns_structured_stale_generation_and_does_not_write(tmp_path):
    from unimem.gateway import Gateway
    from tests.test_gateway import TOKEN, call

    root = str(tmp_path)
    fact = h.add_fact(root, 'Wrist payload limit is 5 kg', scopes=['tenant:a'])[0]
    io.isolation(root, level='serializable', scopes=['tenant:a'])
    reads = io.fetch(root, identities=[fact.id], context=['tenant:a'])['read_set']
    with io.request(read_set=reads, context=['tenant:a']):
        h.update_fact(root, fact.id, statement='Wrist payload limit is 7 kg')
    gateway = Gateway(root, token=TOKEN)
    body = {'items':[{'statement':'Use the lighter grasp'}], 'context':['tenant:a'], 'read_set':reads}
    status, response = call(gateway, 'POST', '/v1/memory/batch', body)
    assert status == 409 and response['error']['code'] == 'STALE_GENERATION'
    details = response['error']['details']
    assert details['options'] == ['regenerate','merge']
    assert details['superseding_facts'][0]['statement'] == 'Wrist payload limit is 7 kg'
    assert len(h.load_facts(root)) == 1
    body['read_set'] = io.fetch(root, identities=[fact.id], context=['tenant:a'])['read_set']
    status, response = call(gateway, 'POST', '/v1/memory/batch', body)
    assert status == 200 and response['add_only']
    assert len(h.load_facts(root)) == 2
    body['read_set'] = [{'id':fact.id, 'transaction_time':'bad'}]
    assert call(gateway,'POST','/v1/memory/batch',body)[0] == 400


def test_team_cli_isolation_read_and_lease(tmp_path, capsys):
    import json

    from unimem.cli import main

    root = str(tmp_path)
    fact = h.add_fact(root, 'Wrist payload limit is 5 kg')[0]
    assert main(['team','isolation','{"level":"serializable"}','--dest',root]) == 0
    assert json.loads(capsys.readouterr().out)['level'] == 'serializable'
    assert main(['team','read',json.dumps({'identities':[fact.id]}),'--dest',root]) == 0
    assert json.loads(capsys.readouterr().out)['read_set'][0]['id'] == fact.id
    assert main(['team','lease','{"work_item":"wrist"}','--dest',root]) == 0
    assert json.loads(capsys.readouterr().out)['fencing_token'] == 1


def test_gateway_fetch_by_id_uses_subtenant_scope(tmp_path):
    from unimem.gateway import Gateway
    from tests.test_gateway import TOKEN, call

    root = str(tmp_path)
    fact = h.add_fact(root, 'Secret medical history', scopes=['tenant:b'])[0]
    gateway = Gateway(root, token=TOKEN)
    body = {'identities':[fact.id], 'context':['tenant:a']}
    status, response = call(gateway, 'POST', '/v1/memory/team-read', body)
    assert status == 403 and 'Secret medical history' not in str(response)
    body['context'] = ['tenant:b']
    assert call(gateway, 'POST', '/v1/memory/team-read', body)[0] == 200


def test_local_and_http_client_forward_and_validate_read_sets(tmp_path, monkeypatch):
    from unimem.client import MemoryClient
    from unimem.gateway import Gateway
    from tests.test_gateway import TOKEN, call

    root = str(tmp_path)
    fact = h.add_fact(root, 'Payload limit is 5 kg', scopes=['tenant:a'])[0]
    io.isolation(root, level='serializable', scopes=['tenant:a'])
    local = MemoryClient(root, context={'tenant':'a'})
    read = local.team_read([fact.id])['read_set']
    assert local.batch([{'statement':'Check wrist load'}], read_set=read)['add_only']
    gateway = Gateway(root, token=TOKEN)
    remote = MemoryClient(url='http://localhost', token=TOKEN, context={'tenant':'a'})
    def http_request(operation, payload):
        status, result = call(gateway, 'POST', '/v1/memory/'+operation, payload)
        assert status == 200, result
        return result
    monkeypatch.setattr(remote, '_request', http_request)
    reads = remote.team_read([fact.id])['read_set']
    assert remote.batch([{'statement':'Inspect the gripper'}], read_set=reads)['add_only']


@pytest.mark.parametrize('sqlite', [False, True])
def test_privacy_erasure_does_not_require_regeneration_or_work_lease(tmp_path, sqlite):
    from unimem import fact_store, privacy_erasure

    root = str(tmp_path)
    if sqlite:
        fact_store.migrate(root)
    fact = h.add_fact(root, 'Erase this private medical detail', scopes=['tenant:a'])[0]
    io.isolation(root, level='sequenced', scopes=['tenant:a'])
    assert privacy_erasure.purge_fact(root, fact.id, reason='owner erasure')['deleted_facts'] == [fact.id]
    assert fact.id not in h.load_facts(root)


def test_erasure_bypass_cannot_add_or_edit_and_requires_revocation(tmp_path):
    from unimem import memory_authority

    root = str(tmp_path)
    fact = h.add_fact(root, 'Secret medical detail')[0]
    io.isolation(root, level='serializable')
    with pytest.raises(PermissionError):
        with io.erasure(root, [fact.id]):
            pass
    memory_authority.record_forgetting(root, fact.id, forgotten=True)
    with pytest.raises(PermissionError):
        with io.erasure(root, [fact.id]):
            with h.mutate_facts(root) as facts:
                facts[fact.id].statement = 'Altered detail'
    assert h.load_facts(root)[fact.id].statement == 'Secret medical detail'


def test_real_otel_span_carries_validated_isolation_and_version(tmp_path, monkeypatch):
    sdk = pytest.importorskip('opentelemetry.sdk.trace')
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from unimem import frontier_protocol as fp
    from unimem import otel_memory, telemetry

    root = str(tmp_path)
    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, '_tracer', lambda:provider.get_tracer('unimem-team-test'))
    otel_memory.configure(root, enabled=True)
    io.isolation(root, level='serializable', scopes=['tenant:a'])
    with io.request(read_set=[], context=['tenant:a']):
        h.append_facts(root, [{'statement':'Inspect wrist load', 'scopes':['tenant:a']}])
    attributes = exporter.get_finished_spans()[-1].attributes
    assert attributes['unimem.isolation.level'] == 'serializable'
    assert attributes['unimem.read.version'] == fp.digest([])
    assert attributes['unimem.read.staleness_ms'] >= 0
    provider.shutdown()


def test_returning_every_space_to_none_restores_legacy_writer(tmp_path):
    root = str(tmp_path)
    io.isolation(root, level='serializable')
    with pytest.raises(pure.StaleGeneration):
        h.add_fact(root, 'Blocked without declared read set')
    io.isolation(root, level='none')
    assert h.add_fact(root, 'Allowed legacy writer again')[1] == 'ADD'


def test_privacy_purge_and_team_forget_use_one_lock_order(tmp_path, monkeypatch):
    import contextlib
    import threading

    from unimem import incident, privacy_erasure

    root = str(tmp_path)
    fact = h.add_fact(root, 'Private detail pending deletion')[0]
    io.isolation(root, level='serializable')
    reads = io.fetch(root, identities=[fact.id])['read_set']
    privacy_holds_incident = threading.Event()
    writer_started = threading.Event()
    writer_has_team = threading.Event()
    failures = []
    original = io._checked
    @contextlib.contextmanager
    def checked(*args, **kwargs):
        if threading.current_thread().name == 'team-forget':
            writer_has_team.set()
        with original(*args, **kwargs):
            yield
    monkeypatch.setattr(io, '_checked', checked)
    def purge():
        try:
            with incident.protect(root):
                privacy_holds_incident.set()
                assert writer_started.wait(1)
                writer_has_team.wait(.1)
                privacy_erasure.purge_fact(root, fact.id, reason='owner privacy request')
        except BaseException as error:
            failures.append(error)
    def forget():
        try:
            assert privacy_holds_incident.wait(1)
            writer_started.set()
            with io.request(read_set=reads):
                h.forget_fact(root, fact.id)
        except pure.StaleGeneration:
            pass  # The owner purge won the serialization order.
        except BaseException as error:
            failures.append(error)
    workers = [threading.Thread(target=purge,daemon=True),
               threading.Thread(target=forget,name='team-forget',daemon=True)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2)
    assert not any(worker.is_alive() for worker in workers), 'inverted incident/team lock order deadlocked'
    assert failures == []
    assert fact.id not in h.load_facts(root)


@pytest.mark.parametrize('module', ['fact_store', 'team_io', 'hierarchical', 'memory_journal', 'client'])
def test_cold_import_order_is_supported(module):
    import subprocess
    import sys

    result = subprocess.run([sys.executable, '-c', 'import unimem.' + module],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
