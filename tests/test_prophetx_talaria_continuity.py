from __future__ import annotations
import json
import pytest
from autosport.prophetx_talaria_continuity import (
    ProphetXTalariaContinuity, ProphetXTalariaContractError,
    ProphetXTalariaGenerationError, TalariaContinuityState,
    TalariaGenerationEvidence, TalariaScope, parse_talaria_frame,
)


def raw(event, data, channel=None):
    outer = {"event": event, "data": json.dumps(data, separators=(",", ":"))}
    if channel is not None: outer["channel"] = channel
    return json.dumps(outer, separators=(",", ":"))


def setup(scopes=(TalariaScope("1"),)):
    m = ProphetXTalariaContinuity()
    g = m.establish_generation(raw("pusher:connection_established", {"socket_id": "11.22"}), config_sha256="a"*64, observed_at="2026-09-22T18:00:00+00:00")
    channels = {f"c{i}": scope for i, scope in enumerate(scopes)}
    m.bind_registration(g, declared_scopes=scopes, channel_scope=channels, channel_limit=10, registration_evidence_sha256="b"*64, observed_at="2026-09-22T18:00:01+00:00")
    return m, g, channels


def handshake(m, g, channels):
    m.handle_frame(g, raw("pusher:signin_success", {}), observed_at="2026-09-22T18:00:02+00:00")
    for i, channel in enumerate(channels, 3):
        m.handle_frame(g, raw("pusher_internal:subscription_succeeded", {}, channel), observed_at=f"2026-09-22T18:00:{i:02d}+00:00")


def rebase(m, g, scopes):
    for i, scope in enumerate(scopes, 10):
        m.acknowledge_rebase(g, scope=scope, snapshot_evidence_sha256=(hex(i)[2:]*64)[:64], snapshot_available_at=f"2026-09-22T18:00:{i:02d}+00:00", observed_at=f"2026-09-22T18:00:{i:02d}+00:00")


@pytest.mark.parametrize("bad", [b"\xff", '{"event":"market_selections","event":"pusher:error","data":"{}"}', '{"event":"market_selections","data":{}}', '{"event":"unknown","data":"{}"}', '{"event":"market_selections","data":"{\\"x\\":NaN}"}', '{"event":"market_selections","data":"{\\"x\\":1,\\"x\\":2}"}'])
def test_strict_double_json_rejects_ambiguous_frames(bad):
    with pytest.raises(ProphetXTalariaContractError): parse_talaria_frame(bad)


def test_connection_frame_parses_and_raw_boundary_reparses():
    parsed = parse_talaria_frame(raw("pusher:connection_established", {"socket_id": "11.22"}))
    assert parsed.data["socket_id"] == "11.22" and len(parsed.frame_sha256) == 64
    m, g, _ = setup()
    with pytest.raises(ProphetXTalariaContractError, match="bytes or text"):
        m.handle_frame(g, parsed, observed_at="2026-09-22T18:00:02+00:00")


def test_signin_and_each_subscription_ack_are_required_before_rebase():
    scopes=(TalariaScope("1"), TalariaScope("1", "player_hits")); m,g,ch=setup(scopes)
    m.handle_frame(g, raw("pusher:signin_success", {}), observed_at="2026-09-22T18:00:02+00:00")
    assert m.state is TalariaContinuityState.SIGNED_IN
    first=next(iter(ch)); m.handle_frame(g, raw("pusher_internal:subscription_succeeded", {}, first), observed_at="2026-09-22T18:00:03+00:00")
    assert m.state is TalariaContinuityState.SIGNED_IN
    second=list(ch)[1]; m.handle_frame(g, raw("pusher_internal:subscription_succeeded", {}, second), observed_at="2026-09-22T18:00:04+00:00")
    assert m.state is TalariaContinuityState.REBASE_REQUIRED


def test_channel_limit_and_exact_coverage_fail_closed():
    m=ProphetXTalariaContinuity(); g=m.establish_generation(raw("pusher:connection_established", {"socket_id":"1"}), config_sha256="a"*64, observed_at="2026-09-22T18:00:00+00:00")
    scopes=(TalariaScope("1"),TalariaScope("2"))
    with pytest.raises(ProphetXTalariaContractError, match="channel_limit"):
        m.bind_registration(g, declared_scopes=scopes, channel_scope={"a":scopes[0],"b":scopes[1]}, channel_limit=1, registration_evidence_sha256="b"*64, observed_at="2026-09-22T18:00:01+00:00")
    with pytest.raises(ProphetXTalariaContractError, match="exactly cover"):
        m.bind_registration(g, declared_scopes=scopes, channel_scope={"a":scopes[0]}, channel_limit=10, registration_evidence_sha256="b"*64, observed_at="2026-09-22T18:00:01+00:00")


def test_market_scope_not_channel_name_drives_identity_and_mismatch_degrades():
    scope=TalariaScope("18756","player_hits"); m,g,ch=setup((scope,)); handshake(m,g,ch); rebase(m,g,(scope,)); channel=next(iter(ch))
    assert m.handle_frame(g, raw("market_selections", {"scope":{"event_id":18756,"sub_type":"player_hits"}}, channel), observed_at="2026-09-22T18:00:20+00:00") == scope
    assert m.state is TalariaContinuityState.REBASE_REQUIRED
    with pytest.raises(ProphetXTalariaContractError, match="scope mismatch"):
        m.handle_frame(g, raw("market_selections", {"scope":{"event_id":"999","sub_type":"player_hits"}}, channel), observed_at="2026-09-22T18:00:21+00:00")
    assert m.state is TalariaContinuityState.DEGRADED


def test_market_before_handshake_is_rejected():
    m,g,ch=setup(); channel=next(iter(ch))
    with pytest.raises(ProphetXTalariaContractError, match="complete handshake"):
        m.handle_frame(g, raw("market_selections", {"scope":{"event_id":"1"}}, channel), observed_at="2026-09-22T18:00:02+00:00")


def test_rebase_is_exact_scope_post_generation_and_not_future():
    subtype=TalariaScope("1","hits"); m,g,ch=setup((subtype,)); handshake(m,g,ch)
    with pytest.raises(ProphetXTalariaContractError, match="preconditions"):
        m.acknowledge_rebase(g, scope=TalariaScope("1"), snapshot_evidence_sha256="c"*64, snapshot_available_at="2026-09-22T18:00:10+00:00", observed_at="2026-09-22T18:00:10+00:00")
    with pytest.raises(ProphetXTalariaContractError, match="pre-generation"):
        m.acknowledge_rebase(g, scope=subtype, snapshot_evidence_sha256="c"*64, snapshot_available_at="2026-09-22T17:59:59+00:00", observed_at="2026-09-22T18:00:10+00:00")
    with pytest.raises(ProphetXTalariaContractError, match="future"):
        m.acknowledge_rebase(g, scope=subtype, snapshot_evidence_sha256="c"*64, snapshot_available_at="2026-09-22T18:00:12+00:00", observed_at="2026-09-22T18:00:11+00:00")


def test_rebased_state_records_digest_but_grants_no_authority():
    scope=TalariaScope("1"); m,g,ch=setup((scope,)); handshake(m,g,ch); rebase(m,g,(scope,)); e=m.evidence()
    assert e.state is TalariaContinuityState.REBASED and e.rebased_snapshots[0][0] == "event:1"
    assert e.gap_free_continuity_proven is e.provider_origin_proven is e.canonical_market_state_authority is e.decision_eligible is e.provider_write_authority is e.real_money_execution is False


def test_positive_authority_flags_are_not_constructor_fields():
    m,_,_=setup(); e=m.evidence(); fields={k:getattr(e,k) for k in ("generation_id","socket_id_sha256","config_sha256","started_at","subscription_digest","coverage_generation_id","registration_evidence_sha256","rebased_snapshots","declared_scopes","acknowledged_scopes","rebase_required_scopes","dirty_scopes","state")}
    with pytest.raises(TypeError): TalariaGenerationEvidence(**fields, provider_write_authority=True)


def test_same_payload_after_rebase_is_new_dirty_signal_not_provider_replay_identity():
    scope=TalariaScope("1"); m,g,ch=setup((scope,)); handshake(m,g,ch); rebase(m,g,(scope,)); channel=next(iter(ch)); update=raw("market_selections", {"scope":{"event_id":"1"},"markets":[]}, channel)
    m.handle_frame(g, update, observed_at="2026-09-22T18:00:20+00:00"); m.acknowledge_rebase(g, scope=scope, snapshot_evidence_sha256="d"*64, snapshot_available_at="2026-09-22T18:00:21+00:00", observed_at="2026-09-22T18:00:21+00:00")
    m.handle_frame(g, update, observed_at="2026-09-22T18:00:22+00:00"); assert m.state is TalariaContinuityState.REBASE_REQUIRED


def test_disconnect_and_config_change_require_new_generation():
    m,g,ch=setup(); handshake(m,g,ch); rebase(m,g,(TalariaScope("1"),)); m.invalidate_config(g,new_config_sha256="c"*64,observed_at="2026-09-22T18:00:20+00:00")
    assert m.state is TalariaContinuityState.DEGRADED
    with pytest.raises(ProphetXTalariaGenerationError, match="reconnect"): m.handle_frame(g, raw("pusher:signin_success",{}), observed_at="2026-09-22T18:00:21+00:00")
    m2,g2,ch2=setup(); m2.disconnect(g2,observed_at="2026-09-22T18:00:02+00:00")
    with pytest.raises(ProphetXTalariaGenerationError, match="no active"): m2.handle_frame(g2, raw("pusher:signin_success",{}), observed_at="2026-09-22T18:00:03+00:00")


def test_new_socket_new_generation_and_no_inherited_coverage():
    m,g,ch=setup(); handshake(m,g,ch); rebase(m,g,(TalariaScope("1"),)); old=m.evidence().coverage_generation_id
    g2=m.establish_generation(raw("pusher:connection_established", {"socket_id":"33.44"}), config_sha256="a"*64, observed_at="2026-09-22T18:01:00+00:00")
    assert g2 != g and m.state is TalariaContinuityState.CONNECTED and m.evidence().coverage_generation_id is None and old is not None


def test_reregistration_changes_coverage_generation_and_forces_rebase():
    m,g,ch=setup((TalariaScope("1"),)); first=m.evidence().coverage_generation_id
    m.bind_registration(g, declared_scopes=(TalariaScope("2"),), channel_scope={"new":TalariaScope("2")}, channel_limit=10, registration_evidence_sha256="c"*64, observed_at="2026-09-22T18:00:02+00:00")
    e=m.evidence(); assert e.coverage_generation_id != first and e.rebase_required_scopes == (TalariaScope("2"),) and e.acknowledged_scopes == ()


def test_clock_rollback_and_secret_shaped_digest_fail_closed():
    m,g,_=setup()
    with pytest.raises(ProphetXTalariaContractError, match="clock moved backwards"): m.invalidate_config(g,new_config_sha256="a"*64,observed_at="2026-09-22T17:59:59+00:00")
    n=ProphetXTalariaContinuity()
    with pytest.raises(ProphetXTalariaContractError, match="SHA-256"): n.establish_generation(raw("pusher:connection_established",{"socket_id":"1"}), config_sha256="Bearer SECRET", observed_at="2026-09-22T18:00:00+00:00")


def test_source_contains_no_production_websocket_target():
    import autosport.prophetx_talaria_continuity as module
    text=open(module.__file__,encoding="utf-8").read(); assert "wss://" not in text and "prophetx.co:443" not in text


def test_dirty_signal_requires_snapshot_at_or_after_rebase_trigger():
    scope = TalariaScope("1")
    m, g, ch = setup((scope,))
    handshake(m, g, ch)
    rebase(m, g, (scope,))
    channel = next(iter(ch))
    update = raw("market_selections", {"scope": {"event_id": "1"}, "markets": [{"v": 1}]}, channel)
    m.handle_frame(g, update, observed_at="2026-09-22T18:00:20+00:00")

    with pytest.raises(ProphetXTalariaContractError, match="rebase trigger"):
        m.acknowledge_rebase(
            g,
            scope=scope,
            snapshot_evidence_sha256="d" * 64,
            snapshot_available_at="2026-09-22T18:00:15+00:00",
            observed_at="2026-09-22T18:00:21+00:00",
        )
    assert m.state is TalariaContinuityState.REBASE_REQUIRED

    m.acknowledge_rebase(
        g,
        scope=scope,
        snapshot_evidence_sha256="e" * 64,
        snapshot_available_at="2026-09-22T18:00:20+00:00",
        observed_at="2026-09-22T18:00:21+00:00",
    )
    assert m.state is TalariaContinuityState.REBASED


def test_second_dirty_signal_raises_rebase_snapshot_lower_bound():
    scope = TalariaScope("1")
    m, g, ch = setup((scope,))
    handshake(m, g, ch)
    rebase(m, g, (scope,))
    channel = next(iter(ch))
    first = raw("market_selections", {"scope": {"event_id": "1"}, "markets": [{"v": 1}]}, channel)
    second = raw("market_selections", {"scope": {"event_id": "1"}, "markets": [{"v": 2}]}, channel)
    m.handle_frame(g, first, observed_at="2026-09-22T18:00:20+00:00")
    m.handle_frame(g, second, observed_at="2026-09-22T18:00:22+00:00")

    with pytest.raises(ProphetXTalariaContractError, match="rebase trigger"):
        m.acknowledge_rebase(
            g,
            scope=scope,
            snapshot_evidence_sha256="d" * 64,
            snapshot_available_at="2026-09-22T18:00:21+00:00",
            observed_at="2026-09-22T18:00:23+00:00",
        )
    m.acknowledge_rebase(
        g,
        scope=scope,
        snapshot_evidence_sha256="e" * 64,
        snapshot_available_at="2026-09-22T18:00:22+00:00",
        observed_at="2026-09-22T18:00:23+00:00",
    )
    assert m.state is TalariaContinuityState.REBASED
