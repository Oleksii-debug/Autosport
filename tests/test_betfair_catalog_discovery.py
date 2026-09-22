from __future__ import annotations

from collections import deque
import hashlib
import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from autosport.betfair_catalog_discovery import (  # noqa: E402
    BetfairCatalogueDiscovery, BetfairCatalogueResponse, Completeness,
)


def sha(v: object) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()


class Fetch:
    def __init__(self, script): self.script, self.calls, self.n = deque(script), [], 0
    def __call__(self, op, params):
        expected, payload = self.script.popleft(); assert op == expected
        self.calls.append((op, json.loads(json.dumps(params)))); self.n += 1
        return BetfairCatalogueResponse(payload, f"r{self.n}", sha([op, params, payload]),
            f"2026-09-22T00:{self.n:02d}:00Z", f"2026-09-22T00:{self.n:02d}:01Z")


def cat(mid, tid="1", tname="Football", eid="e1", ename="A v B", mname="Match Odds"):
    return {"marketId": mid, "marketName": mname, "marketStartTime": "2026-09-22T12:00:00Z",
            "eventType": {"id": tid, "name": tname}, "event": {"id": eid, "name": ename}}


def test_dynamic_multisport_discovery_and_truth_boundary():
    f = Fetch([
        ("listEventTypes", [{"eventType":{"id":"1","name":"Football"}}, {"eventType":{"id":"7522","name":"Basketball"}}]),
        ("listEvents", [{"event":{"id":"e1","name":"A v B"}}]),
        ("listMarketTypes", [{"marketType":"MATCH_ODDS"}]),
        ("listMarketCatalogue", [cat("1.1")]),
        ("listEvents", [{"event":{"id":"e2","name":"C v D"}}]),
        ("listMarketTypes", [{"marketType":"MONEY_LINE"}]),
        ("listMarketCatalogue", [cat("1.2","7522","Basketball","e2","C v D","Moneyline")]),
    ])
    inv = BetfairCatalogueDiscovery(f).discover()
    assert {(m.event_type_id,m.market_id) for m in inv.markets} == {("1","1.1"),("7522","1.2")}
    assert inv.completeness is Completeness.QUERY_SCOPE_COMPLETE
    assert all(v is False for v in inv.truth_boundary.values())


def test_localized_names_do_not_change_canonical_identity():
    def run(locale, tn, en, mn):
        f=Fetch([("listEventTypes",[{"eventType":{"id":"1","name":tn}}]),
                 ("listEvents",[{"event":{"id":"e","name":en}}]),
                 ("listMarketTypes",[{"marketType":"MATCH_ODDS"}]),
                 ("listMarketCatalogue",[cat("1.9","1",tn,"e",en,mn)])])
        return BetfairCatalogueDiscovery(f,locale=locale).discover()
    a=run("en","Football","A B","Match Odds")
    b=run("uk","\u0424\u0443\u0442\u0431\u043e\u043b","\u0410 \u0411","\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442")
    assert a.markets[0].canonical == b.markets[0].canonical
    assert a.semantic_market_set_sha256 == b.semantic_market_set_sha256


def test_cap_hit_is_truncated_unknown_not_complete():
    f=Fetch([("listEventTypes",[{"eventType":{"id":"1","name":"F"}}]),
             ("listEvents",[{"event":{"id":"e1","name":"E"}}]),
             ("listMarketTypes",[{"marketType":"WIN"}]),
             ("listMarketCatalogue",[cat("1.1",tname="F",ename="E"),cat("1.2",tname="F",ename="E")])])
    inv=BetfairCatalogueDiscovery(f,max_results=2).discover()
    assert inv.completeness is Completeness.TRUNCATED_OR_UNKNOWN
    assert len(inv.incomplete_partitions)==1


def test_empty_scope_never_mints_global_absence():
    inv=BetfairCatalogueDiscovery(Fetch([("listEventTypes",[])])).discover()
    assert inv.markets == ()
    assert inv.truth_boundary["global_market_absence_authority"] is False


def test_catalogue_uses_ids_codes_and_safe_projection():
    f=Fetch([("listEventTypes",[{"eventType":{"id":"99","name":"Sport"}}]),
             ("listEvents",[{"event":{"id":"ev","name":"Event"}}]),
             ("listMarketTypes",[{"marketType":"WIN"}]),
             ("listMarketCatalogue",[cat("1.7","99","Sport","ev","Event","Win")])])
    BetfairCatalogueDiscovery(f).discover(); p=f.calls[-1][1]
    assert p["filter"] == {"eventTypeIds":["99"],"eventIds":["ev"],"marketTypeCodes":["WIN"]}
    assert p["marketProjection"] == ["EVENT","EVENT_TYPE","MARKET_START_TIME"]
    assert "MARKET_DESCRIPTION" not in p["marketProjection"]


def test_readonly_operations_only_and_no_secret_fields():
    f=Fetch([("listEventTypes",[])])
    BetfairCatalogueDiscovery(f,base_filter={"marketCountries":["GB"]}).discover()
    assert [x[0] for x in f.calls] == ["listEventTypes"]
    s=json.dumps(f.calls).lower()
    assert not any(x in s for x in ("sessiontoken","appkey","password","authorization"))


@pytest.mark.parametrize("bad",[0,1001,True,1.5])
def test_max_results_contract(bad):
    with pytest.raises(ValueError): BetfairCatalogueDiscovery(lambda *_:None,max_results=bad)


def test_reserved_hierarchy_filter_rejected():
    with pytest.raises(ValueError,match="eventTypeIds"):
        BetfairCatalogueDiscovery(lambda *_:None,base_filter={"eventTypeIds":["1"]})


def test_conflicting_market_id_fails_closed():
    f=Fetch([("listEventTypes",[{"eventType":{"id":"1","name":"F"}}]),
             ("listEvents",[{"event":{"id":"e1","name":"E"}}]),
             ("listMarketTypes",[{"marketType":"A"},{"marketType":"B"}]),
             ("listMarketCatalogue",[cat("1.5",tname="F",ename="E",mname="A")]),
             ("listMarketCatalogue",[cat("1.5",tname="F",ename="E",mname="B")])])
    with pytest.raises(ValueError,match="conflicting duplicate marketId"):
        BetfairCatalogueDiscovery(f).discover()


def test_wrong_catalogue_partition_identity_fails_closed():
    f=Fetch([("listEventTypes",[{"eventType":{"id":"1","name":"F"}}]),
             ("listEvents",[{"event":{"id":"e1","name":"E"}}]),
             ("listMarketTypes",[{"marketType":"A"}]),
             ("listMarketCatalogue",[cat("1.5",tid="2",tname="X",ename="E",mname="A")])])
    with pytest.raises(ValueError,match="mismatches partition"):
        BetfairCatalogueDiscovery(f).discover()


def test_event_id_cannot_cross_sports():
    f=Fetch([("listEventTypes",[{"eventType":{"id":"1","name":"A"}},{"eventType":{"id":"2","name":"B"}}]),
             ("listEvents",[{"event":{"id":"same","name":"X"}}]),("listMarketTypes",[]),
             ("listEvents",[{"event":{"id":"same","name":"X"}}])])
    with pytest.raises(ValueError,match="crossed event types"):
        BetfairCatalogueDiscovery(f).discover()


def test_response_evidence_validation():
    with pytest.raises(ValueError,match="SHA-256"):
        BetfairCatalogueResponse([],"r","bad","2026-09-22T00:00:00Z","2026-09-22T00:00:01Z")
    with pytest.raises(ValueError,match="precedes"):
        BetfairCatalogueResponse([],"r","a"*64,"2026-09-22T00:00:02Z","2026-09-22T00:00:01Z")
    with pytest.raises(ValueError,match="result must be a list"):
        BetfairCatalogueResponse({},"r","a"*64,"2026-09-22T00:00:00Z","2026-09-22T00:00:01Z")


def test_request_id_conflict_fails_closed():
    class Same:
        def __init__(self): self.n=0
        def __call__(self,op,p):
            self.n+=1; payload=[{"eventType":{"id":"1","name":"F"}}] if self.n==1 else []
            return BetfairCatalogueResponse(payload,"same",("a" if self.n==1 else "b")*64,
                f"2026-09-22T00:0{self.n}:00Z",f"2026-09-22T00:0{self.n}:01Z")
    with pytest.raises(ValueError,match="conflicting response bytes"):
        BetfairCatalogueDiscovery(Same()).discover()


def test_backwards_evidence_time_fails_closed():
    class Back:
        def __init__(self): self.n=0
        def __call__(self,op,p):
            self.n+=1
            if self.n==1: return BetfairCatalogueResponse([{"eventType":{"id":"1","name":"F"}}],"r1","a"*64,"2026-09-22T00:10:00Z","2026-09-22T00:10:01Z")
            return BetfairCatalogueResponse([],"r2","b"*64,"2026-09-22T00:09:00Z","2026-09-22T00:09:01Z")
    with pytest.raises(ValueError,match="moved backwards"):
        BetfairCatalogueDiscovery(Back()).discover()


def test_single_use_prevents_evidence_mixing():
    d=BetfairCatalogueDiscovery(Fetch([("listEventTypes",[])])); d.discover()
    with pytest.raises(RuntimeError,match="single-use"): d.discover()
