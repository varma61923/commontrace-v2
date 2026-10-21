"""UniMem optimizer claims never replace randomized delta evidence."""
from __future__ import annotations

import math

import pytest

from tests import test_skill_foundry
from unimem import conformal_gate, experiment, holdout_io
from unimem import optimizer_intake as pure
from unimem import optimizer_io as io

clock = test_skill_foundry.clock


def proposal(**changes):
    return {'base_artifact':'code-v1','delta':'Verify checksum before deployment.', 'optimizer':'generic',
            'claimed_validation_score':999, 'parent':None, **changes}


def prepare(root, clock):
    holdout_io.configure(root, rate=.5, salt='delta-pilot')
    for i in range(80):
        holdout_io.assign_and_log(root, ['pilot'], occasion_id='pilot-'+str(i),rate=.5,salt='delta-pilot',
                                 relevance={'pilot':1}, scorer='unimem-delta')
        holdout_io.record_outcome(root, 'pilot-'+str(i), True)
    conformal_gate.configure(root, alpha=.1, propensity_floor=.5, scorer='unimem-delta')
    clock.tick()


def test_bounded_generic_candidate_and_external_formats():
    first=pure.candidate(proposal())
    assert first['id']==pure.candidate(proposal(claimed_validation_score=-999))['id']
    for format,field in [('gepa','candidates'),('ace','bullets'),('training-free-grpo','experiences'),
                         ('dspy','programs'),('code-diff','diffs')]:
        values=pure.adapt(format,{'base_artifact':'v1',field:['Verify checksum.']})
        assert values[0]['format_status']=='format unverified'
    for bad in (math.nan,math.inf,True):
        with pytest.raises(ValueError):
            pure.candidate(proposal(claimed_validation_score=bad))
    with pytest.raises(ValueError):
        pure.candidate(proposal(delta='a'*60001))
    with pytest.raises(ValueError):
        pure.candidate({**proposal(),'promotion':True})
    nested={}
    for _ in range(20):
        nested={'child':nested}
    with pytest.raises(ValueError):
        pure.adapt('ace',nested)


def test_pareto_uses_randomized_coordinates_and_refuses_claims():
    points=[{'id':'honest','effect':.4,'harm':.1,'supported':True},
            {'id':'high-claim','effect':0.,'harm':.2,'supported':True},
            {'id':'underpowered','effect':1.,'harm':0.,'supported':False}]
    assert pure.pareto(points)==['honest']
    with pytest.raises(ValueError):
        pure.pareto([{**points[0], 'claimed_validation_score':999}])


def test_zero_effect_high_claim_is_not_promoted_and_assignments_are_canonical(tmp_path,clock):
    root=str(tmp_path)
    prepare(root,clock)
    created=io.intake(root,proposal(),training_size=10,minimum_support=1)
    identity=created['candidates'][0]['id']
    clock.tick()
    for i in range(60):
        occasion='delta-'+str(i)
        actual=io.expose(root,[identity],occasion)
        expected=not experiment.is_held_out(identity,occasion,.5,'delta-pilot')
        assert bool(actual['catalog']) is expected
        holdout_io.record_outcome(root,occasion,True)
        clock.tick(1)
    status=io.status(root,identity)
    assert not status['evidence']['promote']
    assert status['feedback']['decision']=='insufficient_evidence'
    with pytest.raises(PermissionError):
        io.status(root,identity,certify=True)
    report=io.frontier(root)
    assert report['points'][0]['effect'] < .4
    assert report['reports'][0]['report']['complete']
    io.expose(root,[identity],'unresolved')
    assert not io.frontier(root)['points'][0]['supported']


def test_ace_prunes_harmful_bullet_despite_reflector_approval(tmp_path,clock):
    root=str(tmp_path)
    prepare(root,clock)
    items=io.intake(root, {'base_artifact':'playbook-v1','bullets':[
        {'delta':'Skip checksum checks.', 'score':999}, {'delta':'Verify checksums.', 'score':-999}]},
        format='ace', training_size=6,minimum_support=1)['candidates']
    clock.tick()
    bad,good=[item['id'] for item in items]
    for i in range(44):
        for identity in (bad,good):
            occasion=identity[-5:]+'-'+str(i)
            actual=io.expose(root,[identity],occasion)
            injected=bool(actual['catalog'])
            holdout_io.record_outcome(root,occasion,not injected if identity==bad else injected)
            clock.tick(1)
    assert io.status(root,bad)['feedback']['prune_bullet']
    assert not io.status(root,good)['feedback']['prune_bullet']
    assert io.expose(root,[bad],'after-prune')['catalog']==[]


def test_retry_cannot_launder_a_smaller_delivered_catalog(tmp_path,clock):
    root=str(tmp_path)
    prepare(root,clock)
    items=io.intake(root, {'base_artifact':'playbook-v1','bullets':['Verify checksums.', 'Inspect the manifest.']},
                    format='ace',training_size=6,minimum_support=1)['candidates']
    clock.tick()
    identities=[item['id'] for item in items]
    first=io.expose(root,identities,'episode')
    assert io.expose(root,identities,'episode')==first
    holdout_io.record_outcome(root,'episode',True)
    assert io.expose(root,identities,'episode')==first
    with pytest.raises(ValueError):
        io.expose(root,identities[:1],'episode')


def test_pinned_gepa_and_dspy_serialization_subsets_discard_runtime_configuration():
    import json
    from pathlib import Path

    fixture = json.loads((Path(__file__).parent/'fixtures'/'optimizer_serialization_contract.json').read_text())
    gepa = pure.adapt('gepa',fixture['gepa'])
    assert len(gepa) == 2 and gepa[1]['parent'] is not None
    assert all(row['schema_commit'] == pure.GEPA_SCHEMA_COMMIT for row in gepa)
    altered = json.loads(json.dumps(fixture['gepa']))
    altered['gepa_result']['val_aggregate_scores'] = [-999,999]
    assert [r['id'] for r in pure.adapt('gepa',altered)] == [r['id'] for r in gepa]
    altered['gepa_result']['parents'] = [[1],[0]]
    with pytest.raises(ValueError):
        pure.adapt('gepa',altered)
    dspy = pure.adapt('dspy',fixture['dspy'])
    assert dspy[0]['schema_commit'] == pure.DSPY_SCHEMA_COMMIT
    assert dspy[0]['dropped_runtime_configuration']
    assert 'must-not-survive' not in str(dspy) and 'untrusted.invalid' not in str(dspy)
    assert 'Verify the release' in dspy[0]['delta']
    fixture['dspy']['dspy_state']['predict']['lm']['api_key'] = 'different-runtime-secret'
    assert pure.adapt('dspy',fixture['dspy'])[0]['id'] == dspy[0]['id']


def test_cli_and_native_mcp_submit_candidates(tmp_path, clock, capsys):
    import json

    from unimem.cli import main
    from unimem.mcp_tools import LOCAL_TOOLS

    root = str(tmp_path)
    prepare(root,clock)
    body = {'proposal':proposal(), 'training_size':6,'minimum_support':1}
    assert main(['skill','optimize','intake',json.dumps(body),'--dest',root]) == 0
    identity = json.loads(capsys.readouterr().out)['candidates'][0]['id']
    clock.tick()
    assert main(['skill','optimize','status',json.dumps({'identity':identity}),'--dest',root]) == 0
    assert json.loads(capsys.readouterr().out)['feedback']['decision'] == 'insufficient_evidence'
    assert main(['skill','optimize','frontier','--dest',root]) == 0
    assert json.loads(capsys.readouterr().out)['frontier'] == []
    pytest.importorskip('mcp')
    from tests.test_mcp_server import call, tool_names
    from unimem import mcp_server

    server = mcp_server.build_server(root)
    assert 'submit_candidate' in LOCAL_TOOLS and 'submit_candidate' in tool_names(server)
    response = call(server,'submit_candidate',proposal=proposal(delta='Inspect release manifest.'))
    assert response['ok'] and response['candidates']
    denied = call(server,'submit_candidate',proposal=proposal(claimed_validation_score='invented'))
    assert not denied['ok']


def test_changed_or_private_sources_cannot_be_delivered(tmp_path, clock):
    from unimem import hierarchical

    root = str(tmp_path)
    prepare(root,clock)
    source = hierarchical.append_facts(root,[{'statement':'Wrist limit is 5 kg','scopes':['tenant:a']}])[0][0]
    with pytest.raises(PermissionError):
        io.intake(root,proposal(),source_ids=[source.id],context=['tenant:b'])
    identity = io.intake(root,proposal(),source_ids=[source.id],context=['tenant:a'])['candidates'][0]['id']
    clock.tick()
    with pytest.raises(PermissionError):
        io.expose(root,[identity],'private',context=['tenant:b'])
    hierarchical.update_fact(root,source.id,statement='Wrist limit is 7 kg')
    with pytest.raises(PermissionError):
        io.expose(root,[identity],'changed',context=['tenant:a'])


def test_positive_delta_certificate_binds_canonical_data_and_current_gate(tmp_path, clock):
    from unimem import promotion, unit_trials

    root = str(tmp_path)
    prepare(root,clock)
    identity = io.intake(root,proposal(),training_size=10,minimum_support=1)['candidates'][0]['id']
    clock.tick()
    for i in range(120):
        occasion = 'promote-'+str(i)
        exposed = io.expose(root,[identity],occasion)
        holdout_io.record_outcome(root,occasion,bool(exposed['catalog']))
        clock.tick(1)
    response = io.status(root,identity,certify=True)
    assert response['evidence']['promote'] and response['evidence']['harm_budget']['observed_ipw_failure'] == 0
    assert promotion.verify(root,response['certificate'],artifact_hash=unit_trials.trial(root,identity)['design']['artifact_hash'])
    conformal_gate.disable(root)
    with pytest.raises(PermissionError):
        io.status(root,identity,certify=True)


def test_seeded_randomized_delta_effect_is_unbiased_and_null_is_not_promoted():
    import random

    rng = random.Random(4602)
    design = {'alpha':.05,'training_size':80,'minimum_support':2,'rate':.2,'margin':0}
    estimates = []
    covered = false_promotions = 0
    for repeat in range(60):
        for effect in (0.,.2):
            rows = []
            for i in range(240):
                injected = rng.random() >= design['rate']
                outcome = rng.random() < .1 + .3*(i%2) + effect*injected
                rows.append({'occasion_id':str(i),'injected':injected,'outcome':int(outcome),
                             'rate':design['rate'],'context':'a' if i%2 else 'b'})
            result = io._evaluate(rows,design)
            if effect == 0:
                false_promotions += result['promote']
            elif result['support_coverage'] == 1:
                estimates.append(result['estimate'])
                low,high = result['confidence_sequence']
                covered += low <= effect <= high
    assert len(estimates) >= 50
    assert sum(estimates)/len(estimates) == pytest.approx(.2,abs=.025)
    assert covered/len(estimates) >= .90
    assert false_promotions <= 6  # 60 null trials; fixed seeded tolerance, not a claimed exact rate.


def test_entire_catalog_is_validated_before_assignment(tmp_path, clock):
    root = str(tmp_path)
    prepare(root,clock)
    identity = io.intake(root,proposal())['candidates'][0]['id']
    clock.tick()
    with pytest.raises(ValueError):
        io.expose(root,[identity,'zzzz-unknown-candidate'],'bad-catalog')
    assert not any(row.occasion_id == 'bad-catalog' for row in holdout_io.read_log(root)[0])


def test_facts_only_capability_cannot_read_optimizer_artifacts(tmp_path, clock):
    from tests.test_capabilities import A_KEY, policy
    from unimem import capabilities, capability_io
    from unimem import frontier_protocol as fp

    root = str(tmp_path)
    prepare(root,clock)
    identity = io.intake(root,proposal())['candidates'][0]['id']
    clock.tick()
    key = tmp_path/'worker.key'
    key.write_bytes(A_KEY)
    key.chmod(0o600)
    capability_io.register_agent(root,'agent-a','worker',secret_file=str(key),clearance=[])
    token = capability_io.grant(root,policy(namespaces=['facts'],classes=['general'],labels=[],delegates=[]))['token']
    signed = capabilities.prove(token,key=A_KEY,algorithm='hmac-sha256',audience='agent-a',
                                tenant=capability_io.tenant(root),request_digest='f'*64,nonce='optimizer-read',now=fp.now())
    authorized = capability_io.authenticate(root,token,signed,request_digest='f'*64,
                                           operation='read',purpose='support',principal='worker')
    with capability_io.activate(authorized):
        with pytest.raises(PermissionError,match='leaf capability'):
            io.status(root,identity)
