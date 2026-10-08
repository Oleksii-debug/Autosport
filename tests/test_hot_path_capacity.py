import dataclasses
import hashlib
import pytest
from autosport.hot_path_latency import HotPathReport, HotPathError, STAGES
from autosport.hot_path_capacity import (
    MAX_CAPACITY_WINDOWS, HotPathCapacityEvidence, summarize_hot_path_windows,
)
SHA = 'a' * 40
OTHER = 'b' * 40

def ok(n=5, *, source=SHA):
    return HotPathReport(source, 'OK', 'WITHIN_BUDGET', tuple((s, n) for s in STAGES), 5*n, 0)

def wait(reason='BACKLOG', *, source=SHA):
    return HotPathReport(source, 'WAIT', reason, (), 3, 9)

def test_bounded_2000_window_campaign_has_exact_repeatable_digest():
    reports = tuple(ok(i%10) for i in range(1, MAX_CAPACITY_WINDOWS+1))
    a = summarize_hot_path_windows(reports, expected_source_sha=SHA)
    b = summarize_hot_path_windows(reports, expected_source_sha=SHA)
    assert a == b
    assert a.complete_count == 2000 and a.wait_count == 0
    assert a.p95_elapsed_ns == 45
    assert a.stage_p95_ns == tuple((s, 9) for s in STAGES)
    assert not a.execution_authority and not a.target_machine_acceptance
    assert len(a.windows_sha256) == 64

def test_backlog_and_stale_waits_do_not_launder_as_success():
    v = summarize_hot_path_windows((ok(),wait(),wait('STALE_SOURCE')), expected_source_sha=SHA)
    assert v.complete_count == 1 and v.wait_count == 2
    assert v.max_backlog == 9
    assert dict(v.wait_reasons)['BACKLOG'] == 1
    assert dict(v.wait_reasons)['STALE_SOURCE'] == 1
    assert v.stage_p95_ns == tuple((s,5) for s in STAGES)
    assert v.p95_elapsed_ns == 25  # includes WAIT windows without filtering

def test_all_wait_remains_nonterminal_with_no_percentiles():
    v = summarize_hot_path_windows((wait(),), expected_source_sha=SHA)
    assert v.complete_count == 0 and v.p95_elapsed_ns == 3 and v.stage_p95_ns == ()

@pytest.mark.parametrize('input', [(), [], (ok(),)*(MAX_CAPACITY_WINDOWS+1), (object(),), (ok(),wait(source=OTHER))])
def test_invalid_size_type_or_revision_fails_closed(input):
    with pytest.raises(HotPathError):
        summarize_hot_path_windows(input, expected_source_sha=SHA)

@pytest.mark.parametrize('sha',['A'*40,'a'*39,'z'*40])
def test_invalid_expected_revision(sha):
    with pytest.raises(HotPathError):
        summarize_hot_path_windows((ok(),),expected_source_sha=sha)

def test_frozen_bypass_cannot_grant_target_authority():
    v = ok()
    object.__setattr__(v,'target_machine_acceptance',True)
    with pytest.raises(HotPathError):
        summarize_hot_path_windows((v,),expected_source_sha=SHA)

@pytest.mark.parametrize('changes',[{'execution_authority':True}, {'target_machine_acceptance':True}, {'wait_count':10}, {'window_count':0}, {'p95_elapsed_ns':9999}, {'p95_elapsed_ns':None}, {'windows_sha256':'bad'}, {'stage_p95_ns':(('evil',5),)*5}])
def test_result_forgery_rejected(changes):
    v = summarize_hot_path_windows((ok(),),expected_source_sha=SHA)
    with pytest.raises(HotPathError):
        dataclasses.replace(v,**changes)


def test_wait_congestion_counts_in_total_percentile():
    reports = tuple(ok(1) for _ in range(10)) + tuple(HotPathReport(SHA,'WAIT','TOTAL_BUDGET',(),1_000_000,0) for _ in range(10))
    v = summarize_hot_path_windows(reports,expected_source_sha=SHA)
    assert v.p95_elapsed_ns == 1_000_000
    assert v.stage_p95_ns == tuple((s,1) for s in STAGES)
    assert v.wait_count == 10


def test_degraded_partial_ingest_latency_is_included_in_stage_p95():
    # A high-latency WAIT at ingest must not vanish from stage telemetry.
    slow_wait = HotPathReport(SHA, 'WAIT', 'STAGE_BUDGET', (('ingest', 1_000_000),), 1_000_001, 0)
    reports = tuple(ok(1) for _ in range(10)) + tuple(slow_wait for _ in range(10))
    v = summarize_hot_path_windows(reports, expected_source_sha=SHA)
    assert v.window_count == 20 and v.wait_count == 10
    assert v.stage_p95_ns[0] == ('ingest', 1_000_000)
    assert v.stage_p95_ns[1:] == tuple((s, 1) for s in STAGES[1:])
    assert v.p95_elapsed_ns == 1_000_001
    assert v.execution_authority is False and v.target_machine_acceptance is False


def test_all_wait_partial_stages_preserve_bounded_prefix_percentiles():
    slow_wait = HotPathReport(SHA, 'WAIT', 'TOTAL_BUDGET', (('ingest', 10_000),), 10_001, 0)
    v = summarize_hot_path_windows((slow_wait, slow_wait), expected_source_sha=SHA)
    assert v.complete_count == 0 and v.wait_count == 2
    assert v.stage_p95_ns == (('ingest', 10_000),)
    assert v.p95_elapsed_ns == 10_001
    assert v.execution_authority is False
