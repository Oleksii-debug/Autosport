"""Append-only corrected-reward generations and exact downstream invalidation.

Structural only: references do not prove settlement/provider origin, replay policy state,
rewrite historical records, authorize promotion, or widen execution authority.
"""
from __future__ import annotations

import hashlib, json, os, sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

CORRECTION_SCHEMA: Final = "autosport.reward_correction_ledger"
CORRECTION_SCHEMA_VERSION: Final = 1
_MAX_FIXED_DECIMAL_CHARS: Final = 512

class RewardCorrectionError(ValueError): pass

def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise RewardCorrectionError(f"{name} must be a non-empty canonical string")
    try: value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc: raise RewardCorrectionError(f"{name} must be valid UTF-8 text") from exc
    return value

def _sha(name: str, value: object) -> str:
    text=_text(name,value)
    if len(text)!=64 or any(c not in "0123456789abcdef" for c in text):
        raise RewardCorrectionError(f"{name} must be lowercase SHA-256 hex")
    return text

def _time(name: str, value: object) -> datetime:
    text=_text(name,value)
    try: parsed=datetime.fromisoformat(text.replace("Z","+00:00"))
    except ValueError as exc: raise RewardCorrectionError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None: raise RewardCorrectionError(f"{name} must be timezone-aware ISO-8601")
    return parsed

def _tid(name: str, value: object) -> str:
    return _time(name,value).astimezone(timezone.utc).isoformat().replace("+00:00","Z")

def _dec(name: str, value: object) -> Decimal:
    if not isinstance(value,Decimal) or not value.is_finite(): raise RewardCorrectionError(f"{name} must be a finite exact Decimal")
    return value

def _fixed_decimal_length(value: Decimal) -> int:
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise RewardCorrectionError("finite Decimal must have an integer exponent")
    digit_count = len(digits)
    sign_count = 1 if sign else 0
    is_zero = all(digit == 0 for digit in digits)
    if exponent >= 0:
        return sign_count + 1 if is_zero else sign_count + digit_count + exponent
    return sign_count + max(digit_count + exponent, 1) + 1 + (-exponent)

def _decimal_text(name: str, value: object) -> str:
    exact = _dec(name, value)
    if _fixed_decimal_length(exact) > _MAX_FIXED_DECIMAL_CHARS:
        raise RewardCorrectionError(
            f"{name} fixed-point text exceeds {_MAX_FIXED_DECIMAL_CHARS} characters"
        )
    text = format(exact, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text

def _hash(payload: object) -> str:
    try: raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")
    except (TypeError,ValueError,UnicodeEncodeError) as exc: raise RewardCorrectionError("identity payload is not canonical") from exc
    return hashlib.sha256(raw).hexdigest()

@dataclass(frozen=True, slots=True, order=True)
class EvidenceRef:
    authority_family: str; evidence_id: str; evidence_sha256: str
    def __post_init__(self): _text("authority_family",self.authority_family); _text("evidence_id",self.evidence_id); _sha("evidence_sha256",self.evidence_sha256)
    def payload(self): return [self.authority_family,self.evidence_id,self.evidence_sha256]

@dataclass(frozen=True, slots=True)
class DependencyArtifact:
    artifact: EvidenceRef; dependencies: tuple[EvidenceRef,...]
    def __post_init__(self):
        if not isinstance(self.artifact,EvidenceRef): raise RewardCorrectionError("artifact must be EvidenceRef")
        if type(self.dependencies) is not tuple or not self.dependencies or any(not isinstance(x,EvidenceRef) for x in self.dependencies): raise RewardCorrectionError("dependencies must be a non-empty tuple of EvidenceRef values")
        if self.dependencies != tuple(sorted(self.dependencies)): raise RewardCorrectionError("dependencies must be sorted")
        keys=[(x.authority_family,x.evidence_id) for x in self.dependencies]
        if len(keys)!=len(set(keys)): raise RewardCorrectionError("dependency identities must be unique")
        if (self.artifact.authority_family,self.artifact.evidence_id) in keys: raise RewardCorrectionError("artifact cannot depend on itself")
    @property
    def record_sha256(self): return _hash({"artifact":self.artifact.payload(),"dependencies":[x.payload() for x in self.dependencies]})

@dataclass(frozen=True, slots=True)
class RewardCorrectionAssertion:
    action_id: str; transition_id: str; superseded_reward: EvidenceRef; corrected_reward: EvidenceRef
    corrected_reward_value: Decimal; corrected_available_at: str; correction_source: EvidenceRef
    generation: int; predecessor_correction_id: str|None=None
    def __post_init__(self):
        _sha("action_id",self.action_id); _sha("transition_id",self.transition_id)
        if not isinstance(self.superseded_reward,EvidenceRef) or not isinstance(self.corrected_reward,EvidenceRef): raise RewardCorrectionError("reward references must be EvidenceRef")
        if self.superseded_reward==self.corrected_reward: raise RewardCorrectionError("correction must produce a distinct reward identity")
        if self.superseded_reward.authority_family!=self.corrected_reward.authority_family: raise RewardCorrectionError("reward correction cannot change reward authority family")
        _dec("corrected_reward_value",self.corrected_reward_value); _time("corrected_available_at",self.corrected_available_at)
        if not isinstance(self.correction_source,EvidenceRef): raise RewardCorrectionError("correction_source must be EvidenceRef")
        if isinstance(self.generation,bool) or not isinstance(self.generation,int) or self.generation<=0: raise RewardCorrectionError("generation must be a positive integer")
        if self.generation==1:
            if self.predecessor_correction_id is not None: raise RewardCorrectionError("first correction cannot have predecessor")
        else: _sha("predecessor_correction_id",self.predecessor_correction_id)
    def payload(self):
        return {"action_id":self.action_id,"transition_id":self.transition_id,"superseded":self.superseded_reward.payload(),"corrected":self.corrected_reward.payload(),"value":_decimal_text("corrected_reward_value",self.corrected_reward_value),"available_at":_tid("corrected_available_at",self.corrected_available_at),"source":self.correction_source.payload(),"generation":self.generation,"predecessor":self.predecessor_correction_id}
    @property
    def correction_id(self): return _hash(self.payload())

@dataclass(frozen=True, slots=True)
class RewardCorrectionReceipt:
    correction_id: str; generation: int; invalidated_artifacts: tuple[EvidenceRef,...]
    source_origin_proven: bool=field(default=False,init=False); policy_replay_authorized: bool=field(default=False,init=False); promotion_authorized: bool=field(default=False,init=False)
    def __post_init__(self):
        _sha("correction_id",self.correction_id)
        if isinstance(self.generation,bool) or not isinstance(self.generation,int) or self.generation<=0: raise RewardCorrectionError("generation must be a positive integer")
        if type(self.invalidated_artifacts) is not tuple or any(not isinstance(x,EvidenceRef) for x in self.invalidated_artifacts): raise RewardCorrectionError("invalidated_artifacts must be a tuple of EvidenceRef values")
        if self.invalidated_artifacts!=tuple(sorted(self.invalidated_artifacts)): raise RewardCorrectionError("invalidated_artifacts must be sorted")

_SCHEMA="""
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE artifacts(family TEXT NOT NULL,id TEXT NOT NULL,sha TEXT NOT NULL,record_sha TEXT NOT NULL,PRIMARY KEY(family,id)) WITHOUT ROWID;
CREATE TABLE dependencies(artifact_family TEXT NOT NULL,artifact_id TEXT NOT NULL,dep_family TEXT NOT NULL,dep_id TEXT NOT NULL,dep_sha TEXT NOT NULL,PRIMARY KEY(artifact_family,artifact_id,dep_family,dep_id),FOREIGN KEY(artifact_family,artifact_id) REFERENCES artifacts(family,id)) WITHOUT ROWID;
CREATE TABLE corrections(correction_id TEXT PRIMARY KEY,action_id TEXT NOT NULL,transition_id TEXT NOT NULL,generation INTEGER NOT NULL,predecessor TEXT,sup_family TEXT NOT NULL,sup_id TEXT NOT NULL,sup_sha TEXT NOT NULL,new_family TEXT NOT NULL,new_id TEXT NOT NULL,new_sha TEXT NOT NULL,corrected_reward_value TEXT NOT NULL,available_at TEXT NOT NULL,source_family TEXT NOT NULL,source_id TEXT NOT NULL,source_sha TEXT NOT NULL,record_sha TEXT NOT NULL,UNIQUE(action_id,transition_id,generation)) WITHOUT ROWID;
CREATE TABLE invalidations(correction_id TEXT NOT NULL,artifact_family TEXT NOT NULL,artifact_id TEXT NOT NULL,artifact_sha TEXT NOT NULL,PRIMARY KEY(correction_id,artifact_family,artifact_id),FOREIGN KEY(correction_id) REFERENCES corrections(correction_id)) WITHOUT ROWID;
CREATE INDEX dependencies_by_dependency ON dependencies(dep_family,dep_id,dep_sha);
CREATE INDEX invalidations_by_artifact ON invalidations(artifact_family,artifact_id,artifact_sha);
CREATE INDEX corrections_by_superseded ON corrections(sup_family,sup_id,sup_sha);
CREATE INDEX corrections_by_corrected ON corrections(new_family,new_id,new_sha);
CREATE TRIGGER artifacts_no_update BEFORE UPDATE ON artifacts BEGIN SELECT RAISE(ABORT,'reward correction artifacts are immutable'); END;
CREATE TRIGGER artifacts_no_delete BEFORE DELETE ON artifacts BEGIN SELECT RAISE(ABORT,'reward correction artifacts are immutable'); END;
CREATE TRIGGER dependencies_no_update BEFORE UPDATE ON dependencies BEGIN SELECT RAISE(ABORT,'reward correction dependencies are immutable'); END;
CREATE TRIGGER dependencies_no_delete BEFORE DELETE ON dependencies BEGIN SELECT RAISE(ABORT,'reward correction dependencies are immutable'); END;
CREATE TRIGGER corrections_no_update BEFORE UPDATE ON corrections BEGIN SELECT RAISE(ABORT,'reward corrections are immutable'); END;
CREATE TRIGGER corrections_no_delete BEFORE DELETE ON corrections BEGIN SELECT RAISE(ABORT,'reward corrections are immutable'); END;
CREATE TRIGGER invalidations_no_update BEFORE UPDATE ON invalidations BEGIN SELECT RAISE(ABORT,'reward invalidations are immutable'); END;
CREATE TRIGGER invalidations_no_delete BEFORE DELETE ON invalidations BEGIN SELECT RAISE(ABORT,'reward invalidations are immutable'); END;
"""
_TRIGGER_SQL={
    "artifacts_no_update": "CREATE TRIGGER artifacts_no_update BEFORE UPDATE ON artifacts BEGIN SELECT RAISE(ABORT,'reward correction artifacts are immutable'); END",
    "artifacts_no_delete": "CREATE TRIGGER artifacts_no_delete BEFORE DELETE ON artifacts BEGIN SELECT RAISE(ABORT,'reward correction artifacts are immutable'); END",
    "dependencies_no_update": "CREATE TRIGGER dependencies_no_update BEFORE UPDATE ON dependencies BEGIN SELECT RAISE(ABORT,'reward correction dependencies are immutable'); END",
    "dependencies_no_delete": "CREATE TRIGGER dependencies_no_delete BEFORE DELETE ON dependencies BEGIN SELECT RAISE(ABORT,'reward correction dependencies are immutable'); END",
    "corrections_no_update": "CREATE TRIGGER corrections_no_update BEFORE UPDATE ON corrections BEGIN SELECT RAISE(ABORT,'reward corrections are immutable'); END",
    "corrections_no_delete": "CREATE TRIGGER corrections_no_delete BEFORE DELETE ON corrections BEGIN SELECT RAISE(ABORT,'reward corrections are immutable'); END",
    "invalidations_no_update": "CREATE TRIGGER invalidations_no_update BEFORE UPDATE ON invalidations BEGIN SELECT RAISE(ABORT,'reward invalidations are immutable'); END",
    "invalidations_no_delete": "CREATE TRIGGER invalidations_no_delete BEFORE DELETE ON invalidations BEGIN SELECT RAISE(ABORT,'reward invalidations are immutable'); END",
}
_TRIGGERS=set(_TRIGGER_SQL)
_INDEXES={"dependencies_by_dependency","invalidations_by_artifact","corrections_by_superseded","corrections_by_corrected"}

def _normalized_trigger_sql(value:object):
    if not isinstance(value,str): return None
    return " ".join(value.strip().rstrip(";").split())

def _is_canonical_trigger_sql(name:str,value:object):
    expected=_TRIGGER_SQL.get(name)
    if expected is None: return False
    return _normalized_trigger_sql(value)==_normalized_trigger_sql(expected)

def _discard_owned_ledger_path(path:Path):
    try: path.unlink(missing_ok=True)
    except OSError: pass

def _reserve_new_ledger_path(path:Path):
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError as exc:
        raise RewardCorrectionError("reward correction ledger already exists") from exc
    except OSError as exc:
        raise RewardCorrectionError("cannot reserve reward correction ledger path") from exc
    try: os.close(fd)
    except OSError as exc:
        _discard_owned_ledger_path(path)
        raise RewardCorrectionError("cannot reserve reward correction ledger path") from exc

class RewardCorrectionLedger:
    def __init__(self,path:Path,connection:sqlite3.Connection): self.path=path; self._connection=connection
    @staticmethod
    def _connect(path:Path):
        try:
            c=sqlite3.connect(str(path),timeout=.25,isolation_level=None); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=250"); return c
        except sqlite3.Error as exc: raise RewardCorrectionError("cannot open reward correction ledger") from exc
    @classmethod
    def create(cls,path:str|Path):
        path=Path(path)
        if not path.parent.is_dir(): raise RewardCorrectionError("reward correction ledger parent must already exist")
        _reserve_new_ledger_path(path)
        c=None
        try:
            c=cls._connect(path)
            c.executescript(_SCHEMA); c.executemany("INSERT INTO metadata VALUES (?,?)",(("schema",CORRECTION_SCHEMA),("schema_version",str(CORRECTION_SCHEMA_VERSION))))
            obj=cls(path,c); obj.verify_integrity(); return obj
        except Exception:
            if c is not None: c.close()
            _discard_owned_ledger_path(path)
            raise
    @classmethod
    def open(cls,path:str|Path):
        path=Path(path)
        if not path.is_file(): raise RewardCorrectionError("reward correction ledger does not exist")
        c=cls._connect(path)
        try: obj=cls(path,c); obj.verify_integrity(); return obj
        except Exception: c.close(); raise
    def close(self): self._connection.close()
    def __enter__(self): return self
    def __exit__(self,*_): self.close()
    def _rollback(self):
        try: self._connection.execute("ROLLBACK")
        except sqlite3.Error: pass
    def _deps(self,a:EvidenceRef):
        rows=self._connection.execute("SELECT dep_family,dep_id,dep_sha FROM dependencies WHERE artifact_family=? AND artifact_id=? ORDER BY dep_family,dep_id,dep_sha",(a.authority_family,a.evidence_id)).fetchall()
        return tuple(EvidenceRef(r[0],r[1],r[2]) for r in rows)
    def _superseded(self,r:EvidenceRef):
        return self._connection.execute("SELECT 1 FROM corrections WHERE sup_family=? AND sup_id=? AND sup_sha=? LIMIT 1",r.payload()).fetchone() is not None
    def is_invalidated(self,a:EvidenceRef):
        if not isinstance(a,EvidenceRef): raise TypeError("artifact must be EvidenceRef")
        return self._connection.execute("SELECT 1 FROM invalidations WHERE artifact_family=? AND artifact_id=? AND artifact_sha=? LIMIT 1",a.payload()).fetchone() is not None
    def append_artifact(self,node:DependencyArtifact):
        if not isinstance(node,DependencyArtifact): raise TypeError("node must be DependencyArtifact")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            old=self._connection.execute("SELECT sha,record_sha FROM artifacts WHERE family=? AND id=?",(node.artifact.authority_family,node.artifact.evidence_id)).fetchone()
            if old:
                if old[0]!=node.artifact.evidence_sha256 or old[1]!=node.record_sha256: raise RewardCorrectionError("artifact identity is already bound differently")
                self._connection.execute("COMMIT"); return node.artifact
            if self._connection.execute("SELECT 1 FROM dependencies WHERE dep_family=? AND dep_id=? LIMIT 1",(node.artifact.authority_family,node.artifact.evidence_id)).fetchone(): raise RewardCorrectionError("artifact identity was already consumed as an external dependency")
            for d in node.dependencies:
                if self._superseded(d): raise RewardCorrectionError("artifact cannot depend on a superseded reward")
                if self.is_invalidated(d): raise RewardCorrectionError("artifact cannot depend on invalidated evidence")
                known=self._connection.execute("SELECT sha FROM artifacts WHERE family=? AND id=?",(d.authority_family,d.evidence_id)).fetchone()
                if known and known[0]!=d.evidence_sha256: raise RewardCorrectionError("dependency digest conflicts with registered artifact")
            self._connection.execute("INSERT INTO artifacts VALUES (?,?,?,?)",(*node.artifact.payload(),node.record_sha256))
            self._connection.executemany("INSERT INTO dependencies VALUES (?,?,?,?,?)",((node.artifact.authority_family,node.artifact.evidence_id,*d.payload()) for d in node.dependencies))
            self._connection.execute("COMMIT"); return node.artifact
        except RewardCorrectionError: self._rollback(); raise
        except sqlite3.Error as exc: self._rollback(); raise RewardCorrectionError("cannot append dependency artifact") from exc
    def _from_row(self,r:sqlite3.Row):
        try: value=Decimal(r["corrected_reward_value"])
        except (InvalidOperation,TypeError) as exc: raise RewardCorrectionError("stored corrected reward is invalid") from exc
        if not value.is_finite() or _decimal_text("stored corrected reward",value)!=r["corrected_reward_value"]: raise RewardCorrectionError("stored corrected reward is not canonical Decimal text")
        a=RewardCorrectionAssertion(r["action_id"],r["transition_id"],EvidenceRef(r["sup_family"],r["sup_id"],r["sup_sha"]),EvidenceRef(r["new_family"],r["new_id"],r["new_sha"]),value,r["available_at"],EvidenceRef(r["source_family"],r["source_id"],r["source_sha"]),r["generation"],r["predecessor"])
        if a.correction_id!=r["correction_id"] or a.correction_id!=r["record_sha"]: raise RewardCorrectionError("stored reward correction identity mismatch")
        return a
    def latest_correction(self,*,action_id:str,transition_id:str):
        row=self._connection.execute("SELECT * FROM corrections WHERE action_id=? AND transition_id=? ORDER BY generation DESC LIMIT 1",(_sha("action_id",action_id),_sha("transition_id",transition_id))).fetchone()
        return None if row is None else self._from_row(row)
    def _lineage_guard(self,a:RewardCorrectionAssertion):
        new=a.corrected_reward
        if self._connection.execute("SELECT 1 FROM corrections WHERE (new_family=? AND new_id=? AND new_sha=?) OR (sup_family=? AND sup_id=? AND sup_sha=?) LIMIT 1",(*new.payload(),*new.payload())).fetchone(): raise RewardCorrectionError("corrected reward identity already belongs to durable correction history")
        sup=a.superseded_reward
        rows=self._connection.execute("SELECT action_id,transition_id FROM corrections WHERE (new_family=? AND new_id=? AND new_sha=?) OR (sup_family=? AND sup_id=? AND sup_sha=?)",(*sup.payload(),*sup.payload())).fetchall()
        if a.generation==1 and rows: raise RewardCorrectionError("superseded reward identity already belongs to durable correction history")
        if any((r[0],r[1])!=(a.action_id,a.transition_id) for r in rows): raise RewardCorrectionError("reward correction cannot branch across action/transition lineages")
    def _validate_chain(self,a:RewardCorrectionAssertion):
        prior=self.latest_correction(action_id=a.action_id,transition_id=a.transition_id)
        if prior is None:
            if a.generation!=1 or a.predecessor_correction_id is not None: raise RewardCorrectionError("first durable correction must be generation 1")
            return
        if a.generation!=prior.generation+1: raise RewardCorrectionError("correction generation must advance exactly once")
        if a.predecessor_correction_id!=prior.correction_id: raise RewardCorrectionError("correction predecessor does not match latest generation")
        if a.superseded_reward!=prior.corrected_reward: raise RewardCorrectionError("next correction must supersede the prior corrected reward")
        if _time("current",a.corrected_available_at)<_time("prior",prior.corrected_available_at): raise RewardCorrectionError("correction availability cannot move backward")
    def _closure(self,root:EvidenceRef):
        rows=self._connection.execute("""WITH RECURSIVE invalid(family,id,sha) AS (VALUES(?,?,?) UNION SELECT a.family,a.id,a.sha FROM artifacts a JOIN dependencies d ON a.family=d.artifact_family AND a.id=d.artifact_id JOIN invalid i ON d.dep_family=i.family AND d.dep_id=i.id AND d.dep_sha=i.sha) SELECT family,id,sha FROM invalid WHERE NOT(family=? AND id=? AND sha=?) ORDER BY family,id,sha""",(*root.payload(),*root.payload())).fetchall()
        return tuple(EvidenceRef(*r) for r in rows)
    def _invalidated(self,cid:str):
        rows=self._connection.execute("SELECT artifact_family,artifact_id,artifact_sha FROM invalidations WHERE correction_id=? ORDER BY artifact_family,artifact_id,artifact_sha",(_sha("correction_id",cid),)).fetchall(); return tuple(EvidenceRef(*r) for r in rows)
    def append_correction(self,a:RewardCorrectionAssertion):
        if not isinstance(a,RewardCorrectionAssertion): raise TypeError("assertion must be RewardCorrectionAssertion")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row=self._connection.execute("SELECT * FROM corrections WHERE correction_id=?",(a.correction_id,)).fetchone()
            if row:
                if self._from_row(row)!=a: raise RewardCorrectionError("correction identity payload is corrupt")
                rec=RewardCorrectionReceipt(a.correction_id,a.generation,self._invalidated(a.correction_id)); self._connection.execute("COMMIT"); return rec
            self._lineage_guard(a); self._validate_chain(a)
            self._connection.execute("INSERT INTO corrections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(a.correction_id,a.action_id,a.transition_id,a.generation,a.predecessor_correction_id,*a.superseded_reward.payload(),*a.corrected_reward.payload(),_decimal_text("corrected_reward_value",a.corrected_reward_value),_tid("corrected_available_at",a.corrected_available_at),*a.correction_source.payload(),a.correction_id))
            invalid=self._closure(a.superseded_reward)
            self._connection.executemany("INSERT INTO invalidations VALUES (?,?,?,?)",((a.correction_id,*x.payload()) for x in invalid))
            self._connection.execute("COMMIT"); return RewardCorrectionReceipt(a.correction_id,a.generation,invalid)
        except RewardCorrectionError: self._rollback(); raise
        except sqlite3.IntegrityError as exc: self._rollback(); raise RewardCorrectionError("correction generation conflicts with durable history") from exc
        except sqlite3.Error as exc: self._rollback(); raise RewardCorrectionError("cannot append reward correction") from exc
    def verify_integrity(self):
        try:
            if dict(self._connection.execute("SELECT key,value FROM metadata"))!={"schema":CORRECTION_SCHEMA,"schema_version":str(CORRECTION_SCHEMA_VERSION)}: raise RewardCorrectionError("reward correction metadata mismatch")
            triggers={r[0]:r[1] for r in self._connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
            if set(triggers)!=_TRIGGERS or any(not _is_canonical_trigger_sql(name,triggers[name]) for name in _TRIGGERS): raise RewardCorrectionError("reward correction immutability triggers mismatch")
            indexes={r[0] for r in self._connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
            if indexes!=_INDEXES: raise RewardCorrectionError("reward correction performance indexes mismatch")
            arts={}
            for r in self._connection.execute("SELECT family,id,sha,record_sha FROM artifacts"):
                a=EvidenceRef(r[0],r[1],r[2]); node=DependencyArtifact(a,self._deps(a))
                if node.record_sha256!=r[3]: raise RewardCorrectionError("artifact dependency record digest mismatch")
                arts[(a.authority_family,a.evidence_id)]=a
            for a in arts.values():
                for d in self._deps(a):
                    known=arts.get((d.authority_family,d.evidence_id))
                    if known is not None and known!=d: raise RewardCorrectionError("registered dependency digest mismatch")
            prior={}; seen_rewards=set()
            for r in self._connection.execute("SELECT * FROM corrections ORDER BY action_id,transition_id,generation"):
                a=self._from_row(r); key=(a.action_id,a.transition_id); p=prior.get(key)
                sup_key=tuple(a.superseded_reward.payload()); new_key=tuple(a.corrected_reward.payload())
                if p is None:
                    if a.generation!=1 or a.predecessor_correction_id is not None: raise RewardCorrectionError("reward correction chain does not start at generation 1")
                    if sup_key in seen_rewards: raise RewardCorrectionError("reward correction reuses durable reward identity across lineages")
                    seen_rewards.add(sup_key)
                elif a.generation!=p.generation+1 or a.predecessor_correction_id!=p.correction_id or a.superseded_reward!=p.corrected_reward or _time("current",a.corrected_available_at)<_time("prior",p.corrected_available_at): raise RewardCorrectionError("reward correction chain integrity mismatch")
                if new_key in seen_rewards: raise RewardCorrectionError("reward correction reuses durable reward identity")
                seen_rewards.add(new_key)
                if self._invalidated(a.correction_id)!=self._closure(a.superseded_reward): raise RewardCorrectionError("reward correction invalidation closure mismatch")
                prior[key]=a
            if self._connection.execute("SELECT 1 FROM invalidations i LEFT JOIN corrections c ON c.correction_id=i.correction_id WHERE c.correction_id IS NULL LIMIT 1").fetchone(): raise RewardCorrectionError("orphan correction invalidation evidence")
            for a in arts.values():
                for d in self._deps(a):
                    if self._superseded(d) and not self.is_invalidated(a): raise RewardCorrectionError("artifact depending on superseded reward lacks invalidation evidence")
                    if self.is_invalidated(d) and not self.is_invalidated(a): raise RewardCorrectionError("artifact depending on invalidated evidence lacks invalidation evidence")
        except RewardCorrectionError: raise
        except sqlite3.Error as exc: raise RewardCorrectionError("reward correction ledger is unreadable") from exc
