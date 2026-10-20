"""UniMem transfer evidence and factorial forgetting use both directional factors."""
from __future__ import annotations

import math
import random

import pytest

from commontrace import sentinel


def rows(rng, effect, context, n=200):
    result = []
    for i in range(n):
        injected = rng.random() < .5
        y = rng.random() < (.5+effect/2 if injected else .5-effect/2)
        result.append({'occasion_id':context+':'+str(i),'injected':injected,'outcome':int(y),
                       'rate':.5,'context':context})
    return result


def test_seeded_transfer_quarantine_power_and_false_alarm_rate():
    rng = random.Random(225)
    poison = benign = 0
    for _ in range(60):
        origin = rows(rng,.8,'origin')
        poisoned = sentinel.transfer(origin+rows(rng,-.8,'other'),origin='origin',segments=['origin','other'])
        harmless = sentinel.transfer(origin+rows(rng,.3,'other'),origin='origin',segments=['origin','other'])
        poison += poisoned['quarantine']
        benign += harmless['quarantine']
    assert poison >= 50
    assert benign/60 <= .05  # Seeded regression check; the guarantee comes from the test assumptions.


def test_origin_benefit_alone_does_not_quarantine_other_segments():
    rng = random.Random(28)
    result = sentinel.transfer(rows(rng,.9,'origin')+rows(rng,.4,'other'),
                               origin='origin',segments=['origin','other'])
    assert result['tests'][0]['origin_benefit_e'] > 20
    assert not result['quarantine']
    assert result['tests'][0]['crossing_e'] <= result['tests'][0]['transfer_harm_e']


def test_factorial_forgetting_uses_the_product_propensity_bound_and_range():
    observations = []
    # Balanced factorial canary effect is +1 in control and -1 under update.
    for i in range(160):
        update, canary = bool(i%2),bool((i//2)%2)
        observations.append({'occasion_id':str(i),'update':update,'canary':canary,
                             'outcome':int(canary != update),'update_rate':.5,'canary_rate':.5})
    result = sentinel.interference(observations,tolerance=.1,minimum_support=5)
    assert result['estimate'] == -2
    assert result['score_bound'] == 4
    assert result['regression'] and not result['allow']
    assert result['confidence_sequence'][0] == -2
    unequal = [{**row,'update_rate':.2,'canary_rate':.3} for row in observations]
    assert sentinel.interference(unequal)['score_bound'] == pytest.approx(1/(.2*.3))


def test_seeded_factorial_interaction_estimate_and_interval_cover():
    rng = random.Random(331)
    estimates = []
    covered = 0
    for repeat in range(60):
        observations = []
        for i in range(360):
            u,c = rng.random()<.7,rng.random()<.6
            y = rng.random() < .2+.3*c+.1*u-.4*u*c
            observations.append({'occasion_id':str(i),'update':u,'canary':c,'outcome':int(y),
                                 'update_rate':.3,'canary_rate':.4})
        result = sentinel.interference(observations)
        estimates.append(result['estimate'])
        low,high = result['confidence_sequence']
        covered += low <= -.4 <= high
    assert sum(estimates)/len(estimates) == pytest.approx(-.4,abs=.04)
    assert covered >= 54


def test_four_frozen_drift_pathways_alarm_without_claiming_cause():
    observations = [{'occasion_id':str(i),'failure':1,'pathway':'tool'} for i in range(120)]
    result = sentinel.drift(observations,baseline_failure=.05)
    assert result['pathways']['tool']['alarm']
    assert not any(result['pathways'][p]['alarm'] for p in ('model','memory','workflow'))
    assert 'heuristic' in result['classification']


def test_unchanged_balanced_canary_can_clear_the_default_tolerance():
    # Enough support must be available to establish a small non-regression
    # margin. Merely failing to reject harm on a short trial cannot do so.
    observations = [{'occasion_id':str(i),'update':bool(i%2),'canary':bool(i//2%2),
                     'outcome':int(bool(i//2%2)),'update_rate':.5,'canary_rate':.5}
                    for i in range(30000)]
    assert not sentinel.interference(observations[:100])['allow']
    result = sentinel.interference(observations)
    assert result['allow'] and result['confidence_sequence'][0] >= -.1


@pytest.mark.parametrize('bad', [math.nan,math.inf,-.1,1.1,True])
def test_transfer_rejects_invalid_rates(bad):
    with pytest.raises(ValueError):
        sentinel.transfer([{'occasion_id':'x','injected':True,'outcome':1,'rate':bad,'context':'origin'}],
                           origin='origin',segments=['origin','other'])
