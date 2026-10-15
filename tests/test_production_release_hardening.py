import concurrent.futures
import socket
import urllib.error

import pytest

from commontrace import retry, telemetry


@pytest.mark.parametrize('error', [TimeoutError('after commit'), ConnectionResetError('response lost'),
                                  urllib.error.URLError(TimeoutError('response lost')), OSError('ambiguous')])
def test_ambiguous_write_failure_is_not_replayed(error):
    committed = []

    def write():
        committed.append('persisted')
        raise error

    value, result = retry.call_with_retries(write, idempotent=False, sleep=lambda _: None)
    assert value is None and result is error and committed == ['persisted']
    assert retry.is_retryable(error, idempotent=True)[0]


@pytest.mark.parametrize('error', [ConnectionRefusedError(), socket.gaierror(),
                                  urllib.error.URLError(ConnectionRefusedError())])
def test_pre_connection_failure_can_recover_without_duplicate_writes(error):
    assert retry.is_retryable(error, idempotent=False)[0]


def test_metrics_have_shared_capacity_and_preserve_existing_series(monkeypatch):
    telemetry.reset()
    monkeypatch.setattr(telemetry, '_MAX_SERIES', 16)
    try:
        telemetry.count('existing', value=1)
        telemetry.observe('existing_ms', 1)
        def emit(number):
            telemetry.count('requests', user=str(number))
            telemetry.observe('latency', number, user=str(number))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(emit, range(500)))
        assert len(telemetry._COUNTERS) + len(telemetry._HISTOGRAMS) == 16
        telemetry.count('existing', value=2)
        assert telemetry._COUNTERS[('existing', ())] == 3
        snapshot = telemetry.metrics()
        assert sum(row['value'] for row in snapshot['counters']
                   if row['name'] == 'commontrace_telemetry_dropped_series') == 986
        assert 'commontrace_telemetry_dropped_series_total' in telemetry.prometheus()
    finally:
        telemetry.reset()


def test_prometheus_renders_outside_writer_lock(monkeypatch):
    telemetry.reset()
    telemetry.count('requests', endpoint='recall')
    original = telemetry._prom_labels
    def checked(*args):
        assert telemetry._LOCK.acquire(blocking=False)
        telemetry._LOCK.release()
        return original(*args)
    monkeypatch.setattr(telemetry, '_prom_labels', checked)
    try:
        assert 'requests_total' in telemetry.prometheus()
    finally:
        telemetry.reset()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1])
def test_invalid_observations_do_not_poison_scrapes(value):
    telemetry.reset()
    telemetry.count('requests', value)
    telemetry.observe('latency', value)
    telemetry.count('injected\n# TYPE hacked counter')
    telemetry.count('requests', **{'bad-label': 'x'})
    assert telemetry.metrics() == {'counters': [], 'histograms': []}
