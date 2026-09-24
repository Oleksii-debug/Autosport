from decimal import Decimal
import json
import pytest
from autosport.matchbook_market_reader import *

def req(**c):
    v=dict(account_identity="acct-01",environment=MatchbookEnvironment.PRODUCTION,sport_id=15,event_id=101,market_id=202,runner_id=303,side=MatchbookSide.BACK,exchange_type=MatchbookExchangeType.BACK_LAY,odds_type="DECIMAL",currency="GBP",price_mode=MatchbookPriceMode.EXPANDED,depth=3,minimum_liquidity="2",exclude_mirrored_prices=True); v.update(c); return MatchbookMarketReadRequest(**v)
def snap(r=None,**c):
    v=dict(market_state=MatchbookMarketState.OPEN,observed_at="2026-09-23T00:00:00Z",evaluated_at="2026-09-23T00:00:05Z",max_age_seconds="10",raw_response=b'{}',price_rows=(("2.1","50"),("2.2","25"))); v.update(c); return build_matchbook_market_snapshot(r or req(),**v)

def test_explicit_query_and_path():
    x=req(); assert x.path.endswith("/101/markets/202/runners/303/prices")
    assert dict(x.query)=={"currency":"GBP","depth":"3","exchange-type":"back-lay","exclude-mirrored-prices":"true","minimum-liquidity":"2","odds-type":"DECIMAL","price-mode":"expanded","side":"back"}
@pytest.mark.parametrize(("f","v"),[("account_identity","default"),("sport_id","015"),("event_id",True),("market_id",0),("runner_id",1<<63),("depth",0),("minimum_liquidity",1.5),("currency","JPY"),("odds_type","AMERICAN")])
def test_bad_scope_rejected(f,v):
    with pytest.raises(MatchbookMarketReadError): req(**{f:v})
def test_side_exchange_contract():
    with pytest.raises(MatchbookMarketReadError): req(exchange_type=MatchbookExchangeType.BINARY,side=MatchbookSide.BACK)
    assert not snap(req(exchange_type=MatchbookExchangeType.BINARY,side=MatchbookSide.WIN)).write_authorized
def test_semantic_changes_change_identity():
    xs=[req(account_identity="acct-02"),req(sport_id=16),req(event_id=102),req(market_id=203),req(runner_id=304),req(side=MatchbookSide.LAY),req(currency="EUR"),req(price_mode=MatchbookPriceMode.AGGREGATED),req(depth=2),req(minimum_liquidity="3"),req(exclude_mirrored_prices=False)]
    assert len({req().semantic_sha256,*(x.semantic_sha256 for x in xs)})==12
def test_decimal_canonical_identity(): assert req(minimum_liquidity="2.0").semantic_sha256==req(minimum_liquidity=Decimal("2.000")).semantic_sha256
def test_aggregated_tail_not_raw_depth():
    x=normalize_matchbook_price_levels(req(price_mode=MatchbookPriceMode.AGGREGATED),(('2','10'),('2.1','20'),('2.2','30'))); assert [y.raw_depth_eligible for y in x]==[True,True,False] and x[2].aggregated_tail
def test_expanded_depth_and_exactness():
    assert all(x.raw_depth_eligible for x in normalize_matchbook_price_levels(req(),(('2','1'),('2.1','2'),('2.2','3'))))
    with pytest.raises(MatchbookMarketReadError): normalize_matchbook_price_levels(req(depth=1),(('2','1'),('3','1')))
    with pytest.raises(MatchbookMarketReadError): normalize_matchbook_price_levels(req(),((2.1,'1'),))
    with pytest.raises(MatchbookMarketReadError): normalize_matchbook_price_levels(req(),(('2','1'),('2.0','2')))
def test_time_and_raw_binding():
    with pytest.raises(MatchbookMarketReadError): snap(observed_at="2026-09-23T00:00:06Z")
    with pytest.raises(MatchbookMarketReadError): snap(evaluated_at="2026-09-23T00:00:16Z")
    a=snap(observed_at="2026-09-23T02:00:00+02:00",raw_response=b'{}'); b=snap(raw_response=b'{ }'); assert a.observed_at.endswith('Z') and a.snapshot_sha256!=b.snapshot_sha256
    assert snap(evaluated_at="2026-09-23T00:00:05Z").snapshot_sha256 != snap(evaluated_at="2026-09-23T00:00:06Z").snapshot_sha256
def test_truth_fences_and_market_state():
    x=snap(); assert x.semantically_open and not x.research_usable and not x.actionable and not x.provider_origin_verified and not x.execution_authorized and not x.redistribution_authorized
    for s in (MatchbookMarketState.SUSPENDED,MatchbookMarketState.CLOSED,MatchbookMarketState.GRADED): assert not snap(market_state=s).semantically_open
    with pytest.raises(MatchbookMarketReadError): snap(market_state="unknown")
def test_liquidity_currency_gate():
    require_direct_liquidity_comparability(snap(req(currency="GBP")),snap(req(currency="GBP")))
    with pytest.raises(MatchbookMarketReadError,match="FX"): require_direct_liquidity_comparability(snap(req(currency="GBP")),snap(req(currency="EUR")))
def items(n=5): return tuple(req(runner_id=303+i) for i in range(n))
def _poll_env(tmp_path,monkeypatch):
    workspace=tmp_path/"workspace"
    authority=tmp_path/"authority"
    monkeypatch.setenv("AUTOSPORT_WORKSPACE",str(workspace))
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",str(authority))
    return workspace

def test_poll_budget_restart_and_window(tmp_path,monkeypatch):
    _poll_env(tmp_path,monkeypatch)
    a=plan_matchbook_poll(items(),now="2026-09-23T00:00:00Z",cursor=None,max_requests_per_minute=2,batch_limit=1); assert len(a.request_sha256s)==1
    b=plan_matchbook_poll(items(),now="2026-09-23T00:00:10Z",cursor=a.next_cursor,max_requests_per_minute=2,batch_limit=3); assert len(b.request_sha256s)==1
    c=plan_matchbook_poll(items(),now="2026-09-23T00:00:20Z",cursor=b.next_cursor,max_requests_per_minute=2,batch_limit=2); assert c.request_sha256s==()
    restart=plan_matchbook_poll(items(),now="2026-09-23T00:00:30Z",cursor=None,max_requests_per_minute=2,batch_limit=1); assert restart.request_sha256s==() and restart.next_cursor.used_in_window==2
    d=plan_matchbook_poll(items(),now="2026-09-23T00:01:01Z",cursor=None,max_requests_per_minute=2,batch_limit=1); assert d.request_sha256s==(items()[2].semantic_sha256,)

def test_poll_fail_closed(tmp_path,monkeypatch):
    _poll_env(tmp_path,monkeypatch)
    with pytest.raises(MatchbookMarketReadError): plan_matchbook_poll(items(),now="2026-09-23T00:00:00Z",cursor=None,max_requests_per_minute=701,batch_limit=1)
    x=req()
    with pytest.raises(MatchbookMarketReadError,match="duplicate"): plan_matchbook_poll((x,x),now="2026-09-23T00:00:00Z",cursor=None,max_requests_per_minute=2,batch_limit=1)
    a=plan_matchbook_poll(items(2),now="2026-09-23T00:00:10Z",cursor=None,max_requests_per_minute=2,batch_limit=1)
    with pytest.raises(MatchbookMarketReadError,match="active budget window"): plan_matchbook_poll(items(3),now="2026-09-23T00:00:11Z",cursor=a.next_cursor,max_requests_per_minute=2,batch_limit=1)
    with pytest.raises(MatchbookMarketReadError,match="regressed"): plan_matchbook_poll(items(2),now="2026-09-23T00:00:09Z",cursor=a.next_cursor,max_requests_per_minute=2,batch_limit=1)
    with pytest.raises(MatchbookMarketReadError,match="budget cannot change"): plan_matchbook_poll(items(2),now="2026-09-23T00:00:11Z",cursor=a.next_cursor,max_requests_per_minute=3,batch_limit=1)
    with pytest.raises(MatchbookMarketReadError,match="cursor budget"): MatchbookPollCursor("0"*64,0,"2026-09-23T00:00:00Z",-1)
    with pytest.raises(MatchbookMarketReadError,match="cursor index"): MatchbookPollCursor("0"*64,-1,"2026-09-23T00:00:00Z",0)

def test_caller_cannot_reset_current_window_budget_with_forged_cursor(tmp_path,monkeypatch):
    _poll_env(tmp_path,monkeypatch)
    requests=tuple(req(runner_id=i) for i in range(1,701))
    now="2026-09-23T00:00:00Z"
    exhausted=plan_matchbook_poll(requests,now=now,cursor=None,max_requests_per_minute=700,batch_limit=700)
    assert len(exhausted.request_sha256s)==700 and exhausted.next_cursor.used_in_window==700
    forged=MatchbookPollCursor(exhausted.next_cursor.universe_sha256,exhausted.next_cursor.next_index,exhausted.next_cursor.window_started_at,0)
    with pytest.raises(MatchbookMarketReadError,match="current durable authority"):
        plan_matchbook_poll(requests,now=now,cursor=forged,max_requests_per_minute=700,batch_limit=1)
    resumed=plan_matchbook_poll(requests,now="2026-09-23T00:00:30Z",cursor=None,max_requests_per_minute=700,batch_limit=1)
    assert resumed.request_sha256s==() and resumed.next_cursor.used_in_window==700

def test_persisted_budget_rollback_is_rejected_by_monotonic_authority(tmp_path,monkeypatch):
    workspace=_poll_env(tmp_path,monkeypatch)
    current=plan_matchbook_poll(items(),now="2026-09-23T00:00:00Z",cursor=None,max_requests_per_minute=2,batch_limit=2)
    assert current.next_cursor.used_in_window==2
    state_path=workspace/".matchbook-poll-budget-v1.json"
    raw=json.loads(state_path.read_text(encoding="utf-8"))
    raw["used_in_window"]=0
    state_path.write_text(json.dumps(raw,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
    with pytest.raises(MatchbookMarketReadError,match="monotonic authority"):
        plan_matchbook_poll(items(),now="2026-09-23T00:00:10Z",cursor=None,max_requests_per_minute=2,batch_limit=1)
