"""Fail-closed Matchbook market-read semantics; no network or write authority."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import hashlib, json
from typing import Sequence

EVENT_RPM = 700
I64_MAX = (1 << 63) - 1

class MatchbookMarketReadError(ValueError): pass
class MatchbookEnvironment(StrEnum): PRODUCTION = "production"
class MatchbookSide(StrEnum):
    BACK="back"; LAY="lay"; WIN="win"; LOSE="lose"
class MatchbookExchangeType(StrEnum): BACK_LAY="back-lay"; BINARY="binary"
class MatchbookPriceMode(StrEnum): EXPANDED="expanded"; AGGREGATED="aggregated"
class MatchbookMarketState(StrEnum):
    OPEN="open"; SUSPENDED="suspended"; CLOSED="closed"; GRADED="graded"

def _hash(x):
    return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode()).hexdigest()
def _text(x, name, limit=256):
    if type(x) is not str or not x or x != x.strip() or any(ord(c)<32 for c in x) or len(x.encode())>limit:
        raise MatchbookMarketReadError(f"invalid {name}")
    return x
def _id(x, name):
    if type(x) is int: n=x
    elif type(x) is str and x.isascii() and x.isdigit() and (len(x)==1 or x[0]!="0"): n=int(x)
    else: raise MatchbookMarketReadError(f"invalid {name}")
    if not 0<n<=I64_MAX: raise MatchbookMarketReadError(f"invalid {name}")
    return str(n)
def _dec(x, name, positive=False):
    if isinstance(x,(bool,float)) or not isinstance(x,(Decimal,int,str)):
        raise MatchbookMarketReadError(f"{name} must be exact, never float")
    try: d=Decimal(x)
    except (InvalidOperation,TypeError,ValueError) as e: raise MatchbookMarketReadError(f"invalid {name}") from e
    if not d.is_finite() or (d<=0 if positive else d<0): raise MatchbookMarketReadError(f"invalid {name}")
    return d
def _dtext(d):
    s=format(d,"f"); s=s.rstrip("0").rstrip(".") if "." in s else s
    return "0" if s in {"","-0"} else s
def _utc(x,name):
    try: d=datetime.fromisoformat(_text(x,name,64).replace("Z","+00:00"))
    except ValueError as e: raise MatchbookMarketReadError(f"invalid {name}") from e
    if d.tzinfo is None or d.utcoffset() is None: raise MatchbookMarketReadError(f"{name} needs timezone")
    return d.astimezone(timezone.utc)
def _utctext(d): return d.isoformat().replace("+00:00","Z")

@dataclass(frozen=True,slots=True)
class MatchbookMarketReadRequest:
    account_identity:str; environment:MatchbookEnvironment
    sport_id:str|int; event_id:str|int; market_id:str|int; runner_id:str|int
    side:MatchbookSide; exchange_type:MatchbookExchangeType; odds_type:str; currency:str
    price_mode:MatchbookPriceMode; depth:int; minimum_liquidity:Decimal|int|str
    exclude_mirrored_prices:bool
    def __post_init__(self):
        acct=_text(self.account_identity,"account_identity")
        if acct.lower() in {"default","current","me","unknown","none"}: raise MatchbookMarketReadError("account_identity must be explicit")
        object.__setattr__(self,"account_identity",acct)
        if type(self.environment) is not MatchbookEnvironment: raise MatchbookMarketReadError("invalid environment")
        for f in ("sport_id","event_id","market_id","runner_id"): object.__setattr__(self,f,_id(getattr(self,f),f))
        if type(self.side) is not MatchbookSide or type(self.exchange_type) is not MatchbookExchangeType: raise MatchbookMarketReadError("invalid side/exchange_type")
        sides={MatchbookSide.BACK,MatchbookSide.LAY} if self.exchange_type is MatchbookExchangeType.BACK_LAY else {MatchbookSide.WIN,MatchbookSide.LOSE}
        if self.side not in sides: raise MatchbookMarketReadError("side incompatible with exchange_type")
        if _text(self.odds_type,"odds_type",32).upper() != "DECIMAL": raise MatchbookMarketReadError("explicit DECIMAL odds required")
        object.__setattr__(self,"odds_type","DECIMAL")
        cur=_text(self.currency,"currency",3).upper()
        if cur not in {"USD","EUR","GBP","AUD","CAD","HKD"}: raise MatchbookMarketReadError("unsupported currency")
        object.__setattr__(self,"currency",cur)
        if type(self.price_mode) is not MatchbookPriceMode: raise MatchbookMarketReadError("invalid price_mode")
        if type(self.depth) is not int or isinstance(self.depth,bool) or not 1<=self.depth<=100: raise MatchbookMarketReadError("invalid depth")
        if self.price_mode is MatchbookPriceMode.AGGREGATED and self.depth>3: raise MatchbookMarketReadError("aggregated depth > 3")
        object.__setattr__(self,"minimum_liquidity",_dec(self.minimum_liquidity,"minimum_liquidity"))
        if type(self.exclude_mirrored_prices) is not bool: raise MatchbookMarketReadError("invalid mirrored flag")
    @property
    def path(self): return f"/edge/rest/events/{self.event_id}/markets/{self.market_id}/runners/{self.runner_id}/prices"
    @property
    def query(self): return tuple(sorted((("currency",self.currency),("depth",str(self.depth)),("exchange-type",self.exchange_type.value),("exclude-mirrored-prices",str(self.exclude_mirrored_prices).lower()),("minimum-liquidity",_dtext(self.minimum_liquidity)),("odds-type","DECIMAL"),("price-mode",self.price_mode.value),("side",self.side.value))))
    @property
    def semantic_sha256(self): return _hash({"v":1,"provider":"matchbook","account":self.account_identity,"environment":self.environment.value,"sport":self.sport_id,"event":self.event_id,"market":self.market_id,"runner":self.runner_id,"method":"GET","path":self.path,"query":self.query})

@dataclass(frozen=True,slots=True)
class MatchbookPriceLevel:
    ordinal:int; side:MatchbookSide; odds:Decimal; available_amount:Decimal
    raw_depth_eligible:bool; aggregated_tail:bool
    def payload(self): return [self.ordinal,self.side.value,_dtext(self.odds),_dtext(self.available_amount),self.raw_depth_eligible,self.aggregated_tail]

@dataclass(frozen=True,slots=True)
class MatchbookMarketSnapshot:
    request:MatchbookMarketReadRequest; market_state:MatchbookMarketState
    observed_at:str; evaluated_at:str; raw_response_sha256:str; levels:tuple[MatchbookPriceLevel,...]
    snapshot_sha256:str; freshness_seconds:Decimal
    provider_origin_verified=False; normalized_payload_bound_to_raw_response=False
    execution_authorized=False; write_authorized=False
    redistribution_authorized=False; training_corpus_authorized=False
    @property
    def semantically_open(self): return self.market_state is MatchbookMarketState.OPEN
    @property
    def research_usable(self): return self.semantically_open and self.provider_origin_verified and self.normalized_payload_bound_to_raw_response
    @property
    def actionable(self): return False
    @property
    def raw_depth_complete(self): return bool(self.levels) and all(x.raw_depth_eligible for x in self.levels)
    @property
    def available_liquidity(self): return sum((x.available_amount for x in self.levels),Decimal("0"))

def normalize_matchbook_price_levels(req, rows):
    if type(req) is not MatchbookMarketReadRequest or isinstance(rows,(str,bytes)) or not isinstance(rows,Sequence): raise MatchbookMarketReadError("invalid price rows")
    if len(rows)>req.depth or (req.price_mode is MatchbookPriceMode.AGGREGATED and len(rows)>3): raise MatchbookMarketReadError("rows exceed depth")
    out=[]; seen=set()
    for i,row in enumerate(rows,1):
        if type(row) is not tuple or len(row)!=2: raise MatchbookMarketReadError("row must be tuple")
        odds,amount=_dec(row[0],"odds",True),_dec(row[1],"available_amount")
        key=_dtext(odds)
        if key in seen: raise MatchbookMarketReadError("duplicate price odds")
        seen.add(key); tail=req.price_mode is MatchbookPriceMode.AGGREGATED and i==3
        out.append(MatchbookPriceLevel(i,req.side,odds,amount,not tail,tail))
    return tuple(out)

def build_matchbook_market_snapshot(req,*,market_state,observed_at,evaluated_at,max_age_seconds,raw_response,price_rows):
    if type(req) is not MatchbookMarketReadRequest or type(market_state) is not MatchbookMarketState: raise MatchbookMarketReadError("invalid request/state")
    seen,now=_utc(observed_at,"observed_at"),_utc(evaluated_at,"evaluated_at")
    if seen>now: raise MatchbookMarketReadError("future observation")
    delta=now-seen; age=Decimal(delta.days*86400+delta.seconds)+Decimal(delta.microseconds)/Decimal(1_000_000)
    if age>_dec(max_age_seconds,"max_age_seconds",True): raise MatchbookMarketReadError("stale observation")
    if type(raw_response) is not bytes or not raw_response or len(raw_response)>16*1024*1024: raise MatchbookMarketReadError("invalid raw response")
    levels=normalize_matchbook_price_levels(req,price_rows); raw=hashlib.sha256(raw_response).hexdigest()
    truth=[False,False,False,False,False,False]
    digest=_hash({"v":1,"request":req.semantic_sha256,"state":market_state.value,"observed_at":_utctext(seen),"evaluated_at":_utctext(now),"raw":raw,"levels":[x.payload() for x in levels],"truth":truth})
    return MatchbookMarketSnapshot(req,market_state,_utctext(seen),_utctext(now),raw,levels,digest,age)

def require_direct_liquidity_comparability(a,b):
    if type(a) is not MatchbookMarketSnapshot or type(b) is not MatchbookMarketSnapshot: raise MatchbookMarketReadError("invalid snapshots")
    if a.request.currency!=b.request.currency: raise MatchbookMarketReadError("cross-currency comparison requires authoritative FX evidence")

@dataclass(frozen=True,slots=True)
class MatchbookPollCursor:
    universe_sha256:str; next_index:int; window_started_at:str; used_in_window:int
    def __post_init__(self):
        if type(self.universe_sha256) is not str or len(self.universe_sha256)!=64 or any(c not in "0123456789abcdef" for c in self.universe_sha256): raise MatchbookMarketReadError("invalid cursor universe")
        if type(self.next_index) is not int or isinstance(self.next_index,bool) or self.next_index<0: raise MatchbookMarketReadError("invalid cursor index")
        object.__setattr__(self,"window_started_at",_utctext(_utc(self.window_started_at,"window_started_at")))
        if type(self.used_in_window) is not int or isinstance(self.used_in_window,bool) or not 0<=self.used_in_window<=EVENT_RPM: raise MatchbookMarketReadError("invalid cursor budget")
@dataclass(frozen=True,slots=True)
class MatchbookPollPlan:
    request_sha256s:tuple[str,...]; next_cursor:MatchbookPollCursor

def plan_matchbook_poll(requests,*,now,cursor,max_requests_per_minute,batch_limit):
    if isinstance(requests,(str,bytes)) or not isinstance(requests,Sequence) or not requests: raise MatchbookMarketReadError("empty poll universe")
    items=tuple(requests)
    if any(type(x) is not MatchbookMarketReadRequest for x in items): raise MatchbookMarketReadError("invalid poll request")
    ids=tuple(x.semantic_sha256 for x in items)
    if len(set(ids))!=len(ids): raise MatchbookMarketReadError("duplicate poll request")
    if type(max_requests_per_minute) is not int or isinstance(max_requests_per_minute,bool) or not 1<=max_requests_per_minute<=EVENT_RPM: raise MatchbookMarketReadError("budget must be 1..700")
    if type(batch_limit) is not int or isinstance(batch_limit,bool) or batch_limit<=0: raise MatchbookMarketReadError("invalid batch_limit")
    t=_utc(now,"now"); universe=_hash({"v":1,"provider":"matchbook","requests":ids})
    if cursor is None: start,index,used=t,0,0
    else:
        if type(cursor) is not MatchbookPollCursor or cursor.universe_sha256!=universe: raise MatchbookMarketReadError("cursor belongs to different universe")
        start,index,used=_utc(cursor.window_started_at,"window_started_at"),cursor.next_index%len(items),cursor.used_in_window
        if t<start: raise MatchbookMarketReadError("poll clock regressed")
        if t-start>=timedelta(minutes=1): start,used=t,0
    remaining=max_requests_per_minute-used
    if remaining<0: raise MatchbookMarketReadError("cursor exceeds budget")
    chosen=[]
    for _ in range(min(batch_limit,remaining,len(items))): chosen.append(items[index].semantic_sha256); index=(index+1)%len(items)
    return MatchbookPollPlan(tuple(chosen),MatchbookPollCursor(universe,index,_utctext(start),used+len(chosen)))

__all__=[n for n in globals() if n.startswith("Matchbook")]+["build_matchbook_market_snapshot","normalize_matchbook_price_levels","plan_matchbook_poll","require_direct_liquidity_comparability"]
