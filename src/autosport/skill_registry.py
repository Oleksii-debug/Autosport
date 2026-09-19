"""Governed procedural skills for Autosport research and diagnostics.

This is not an agent framework, scheduler, scientific registry, risk engine, or
execution authority. Dynamic/generated code remains a non-executable candidate.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Final, Mapping

from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock

SCHEMA: Final = "autosport.skill_registry"
SCHEMA_VERSION: Final = 1
AGENT_LOOP_READ_ONLY_AUTHORITY_PROFILE: Final = "agent-loop-read-only-v1"
_HEX: Final = frozenset("0123456789abcdef")
NON_DELEGABLE_MUTATIONS: Final = frozenset({
    "ECONOMIC_GOAL_EXPAND", "RISK_LIMIT_EXPAND", "REAL_MONEY_EXECUTION_ENABLE",
    "PROVIDER_WRITE", "PROMOTION_DECISION", "DECISION_TIME_TRUTH_REWRITE",
    "NEGATIVE_EVIDENCE_DELETE", "DYNAMIC_CODE_EXECUTE",
})

class SkillRegistryError(RuntimeError): pass
class ConflictingSkillDefinitionError(SkillRegistryError): pass
class ConflictingSkillRunError(SkillRegistryError): pass
class SkillPermissionError(SkillRegistryError): pass
class SkillRecoveryRequiredError(SkillRegistryError): pass

class SkillImplementationKind(StrEnum):
    BUILTIN = "BUILTIN"
    REVIEWED_PLUGIN = "REVIEWED_PLUGIN"
    CANDIDATE_DYNAMIC_CODE = "CANDIDATE_DYNAMIC_CODE"

class SkillRunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    INTERRUPTED = "INTERRUPTED"

SkillHandler = Callable[[Mapping[str, Any]], "SkillExecutionResult"]

def _text(v: object, name: str) -> str:
    if type(v) is not str or not v or v != v.strip() or "\x00" in v:
        raise SkillRegistryError(f"{name} must be a non-empty canonical string")
    try: v.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc: raise SkillRegistryError(f"{name} must be valid UTF-8") from exc
    return v

def _sha(v: object, name: str) -> str:
    s = _text(v, name).lower()
    if len(s) != 64 or any(ch not in _HEX for ch in s):
        raise SkillRegistryError(f"{name} must be canonical SHA-256 hex")
    return s

def _time(v: object, name: str) -> str:
    s = _text(v, name)
    try: dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc: raise SkillRegistryError(f"{name} must be ISO-8601") from exc
    if dt.tzinfo is None or dt.utcoffset() is None: raise SkillRegistryError(f"{name} must include timezone")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def _nni(v: object, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise SkillRegistryError(f"{name} must be a non-negative integer")
    return v

def _pos(v: object, name: str) -> int:
    v = _nni(v, name)
    if not v: raise SkillRegistryError(f"{name} must be positive")
    return v

def _texts(v: object, name: str) -> tuple[str, ...]:
    if type(v) is not tuple: raise SkillRegistryError(f"{name} must be a tuple")
    out = tuple(_text(x, f"{name} member") for x in v)
    if out != tuple(sorted(out)) or len(out) != len(set(out)):
        raise SkillRegistryError(f"{name} must be sorted and unique")
    return out

def _prov(v: object) -> tuple[tuple[str, str], ...]:
    if type(v) is not tuple: raise SkillRegistryError("provenance must be a tuple")
    out=[]
    for x in v:
        if type(x) is not tuple or len(x) != 2: raise SkillRegistryError("provenance members must be (key, sha256)")
        out.append((_text(x[0], "provenance key"), _sha(x[1], "provenance sha256")))
    out=tuple(out)
    if out != tuple(sorted(out)) or len({x[0] for x in out}) != len(out):
        raise SkillRegistryError("provenance must be sorted with unique keys")
    return out

def _canon(v: object) -> str:
    try: return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError) as exc: raise SkillRegistryError("payload must be canonical JSON") from exc

def _obj(v: object, name: str) -> dict[str, Any]:
    if type(v) is not dict: raise SkillRegistryError(f"{name} must be a JSON object")
    return json.loads(_canon(v))

def _digest(v: object) -> str: return hashlib.sha256(_canon(v).encode()).hexdigest()
def _builtin_hash(name: str) -> str: return hashlib.sha256(f"autosport-skill-builtin:{name}".encode()).hexdigest()
def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out={}
    for k,v in pairs:
        if k in out: raise SkillRegistryError(f"duplicate JSON object key: {k}")
        out[k]=v
    return out

@dataclass(frozen=True, slots=True)
class SkillAuthorityProfile:
    profile_id: str
    authorities: tuple[str, ...]
    tools: tuple[str, ...] = ()
    def __post_init__(self) -> None:
        _text(self.profile_id, "profile_id")
        _texts(self.authorities, "authorities")
        _texts(self.tools, "tools")
    @property
    def profile_sha256(self) -> str:
        return _digest({
            "schema": "autosport.skill_authority_profile",
            "schema_version": 1,
            "profile_id": self.profile_id,
            "authorities": list(self.authorities),
            "tools": list(self.tools),
        })

_SOURCE_AUTHORITY_PROFILES: Final[dict[str, SkillAuthorityProfile]] = {
    AGENT_LOOP_READ_ONLY_AUTHORITY_PROFILE: SkillAuthorityProfile(
        AGENT_LOOP_READ_ONLY_AUTHORITY_PROFILE,
        ("READ_ONLY_ANALYSIS",),
        (),
    )
}

def _source_authority_profile(profile_id: object) -> SkillAuthorityProfile:
    key = _text(profile_id, "authority_profile_id")
    profile = _SOURCE_AUTHORITY_PROFILES.get(key)
    if profile is None:
        raise SkillPermissionError("unknown source-owned authority profile")
    return profile

@dataclass(frozen=True, slots=True)
class SkillDefinition:
    skill_id: str
    version: str
    capability: str
    purpose: str
    input_schema_sha256: str
    output_schema_sha256: str
    implementation_sha256: str
    implementation_kind: SkillImplementationKind
    required_authorities: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    allowed_mutations: tuple[str, ...] = ()
    forbidden_mutations: tuple[str, ...] = ()
    required_provenance: tuple[str, ...] = ()
    timeout_seconds: int = 30
    compute_budget_units: int = 1
    data_budget_units: int = 0
    ai_budget_units: int = 0
    deterministic: bool = True
    idempotent: bool = True
    acceptance_ids: tuple[str, ...] = ()
    emitted_evidence_types: tuple[str, ...] = ()
    predecessor: str | None = None
    supersedes: tuple[str, ...] = ()
    def __post_init__(self) -> None:
        for n in ("skill_id","version","capability","purpose"): _text(getattr(self,n),n)
        for n in ("input_schema_sha256","output_schema_sha256","implementation_sha256"): _sha(getattr(self,n),n)
        if not isinstance(self.implementation_kind, SkillImplementationKind): raise SkillRegistryError("invalid implementation_kind")
        for n in ("required_authorities","required_tools","allowed_mutations","forbidden_mutations","required_provenance","acceptance_ids","emitted_evidence_types","supersedes"): _texts(getattr(self,n),n)
        _pos(self.timeout_seconds,"timeout_seconds"); _pos(self.compute_budget_units,"compute_budget_units")
        _nni(self.data_budget_units,"data_budget_units"); _nni(self.ai_budget_units,"ai_budget_units")
        if type(self.deterministic) is not bool or type(self.idempotent) is not bool: raise SkillRegistryError("deterministic/idempotent must be boolean")
        if set(self.allowed_mutations) & set(self.forbidden_mutations): raise SkillRegistryError("allowed/forbidden mutations overlap")
        blocked=set(self.allowed_mutations)&NON_DELEGABLE_MUTATIONS
        if blocked: raise SkillRegistryError("skill definition cannot delegate protected mutation(s): "+",".join(sorted(blocked)))
        if self.predecessor is not None: _text(self.predecessor,"predecessor")
        if self.implementation_kind is SkillImplementationKind.CANDIDATE_DYNAMIC_CODE and self.allowed_mutations:
            raise SkillRegistryError("dynamic-code candidates cannot allow mutations")
    def payload(self) -> dict[str, object]:
        return {n:(getattr(self,n).value if n=="implementation_kind" else list(getattr(self,n)) if isinstance(getattr(self,n),tuple) else getattr(self,n)) for n in (
            "skill_id","version","capability","purpose","input_schema_sha256","output_schema_sha256","implementation_sha256","implementation_kind","required_authorities","required_tools","allowed_mutations","forbidden_mutations","required_provenance","timeout_seconds","compute_budget_units","data_budget_units","ai_budget_units","deterministic","idempotent","acceptance_ids","emitted_evidence_types","predecessor","supersedes")}
    @property
    def definition_id(self) -> str: return _digest({"schema":SCHEMA,"kind":"SkillDefinition",**self.payload()})
    @property
    def version_key(self) -> str: return f"{self.skill_id}@{self.version}"

@dataclass(frozen=True, slots=True)
class SkillExecutionResult:
    output: dict[str, Any]
    emitted_evidence: tuple[tuple[str, str], ...] = ()
    used_tools: tuple[str, ...] = ()
    applied_mutations: tuple[str, ...] = ()
    consumed_compute_units: int = 0
    consumed_data_units: int = 0
    consumed_ai_units: int = 0
    research_question_candidate: str | None = None
    def __post_init__(self) -> None:
        _obj(self.output,"output"); _prov(self.emitted_evidence); _texts(self.used_tools,"used_tools"); _texts(self.applied_mutations,"applied_mutations")
        _nni(self.consumed_compute_units,"consumed_compute_units"); _nni(self.consumed_data_units,"consumed_data_units"); _nni(self.consumed_ai_units,"consumed_ai_units")
        if self.research_question_candidate is not None: _text(self.research_question_candidate,"research_question_candidate")

@dataclass(frozen=True, slots=True)
class SkillRun:
    run_id: str; call_id: str; caller_loop_id: str; caller_state_sha256: str; source_sha256: str
    definition_id: str; skill_id: str; version: str; capability: str; status: SkillRunStatus
    requested_at: str; completed_at: str | None; input_sha256: str; output_sha256: str | None
    authority_profile_id: str; authority_profile_sha256: str
    available_authorities: tuple[str,...]; available_tools: tuple[str,...]; requested_mutations: tuple[str,...]
    provenance: tuple[tuple[str,str],...]; requested_compute_units: int; requested_data_units: int; requested_ai_units: int
    consumed_compute_units: int; consumed_data_units: int; consumed_ai_units: int
    used_tools: tuple[str,...]; applied_mutations: tuple[str,...]; emitted_evidence: tuple[tuple[str,str],...]
    research_question_candidate_sha256: str | None; research_question_candidate: str | None
    output: dict[str,Any] | None; error_code: str | None

def _skill_handler_process(handler: SkillHandler, payload: dict[str, Any], sender) -> None:
    """Execute one source-reviewed handler in an isolated killable process."""
    try:
        result = handler(payload)
        sender.send(("OK", result))
    except BaseException as exc:
        try:
            sender.send(("ERROR", exc.__class__.__name__))
        except BaseException:
            pass
    finally:
        sender.close()

class SkillRegistry:
    """Durable exact-version registry with fail-closed invocation semantics."""
    def __init__(self, path: str | Path) -> None:
        self.path=Path(path); self._handlers=dict(_BUILTIN_HANDLERS)
        try: self._read()
        except FileNotFoundError as exc: raise SkillRegistryError("SkillRegistry state is missing") from exc
    @classmethod
    def initialize(cls,path: str|Path)->"SkillRegistry":
        p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
        with WorkspaceEconomicLock(p.parent):
            if not p.exists(): cls._write(p,{"schema":SCHEMA,"schema_version":SCHEMA_VERSION,"definitions":[],"runs":[]})
        return cls(p)
    @staticmethod
    def _without_digest(s): return {k:v for k,v in s.items() if k!="state_sha256"}
    @classmethod
    def _write(cls,p,s):
        s=dict(s); s["state_sha256"]=_digest(cls._without_digest(s)); atomic_write_json(p,s)
    @staticmethod
    def _definition(e: Mapping[str,Any])->SkillDefinition:
        p=e.get("definition")
        if type(p) is not dict: raise SkillRegistryError("definition payload must be object")
        return SkillDefinition(**{**p,"implementation_kind":SkillImplementationKind(p["implementation_kind"]),
            **{n:tuple(p[n]) for n in ("required_authorities","required_tools","allowed_mutations","forbidden_mutations","required_provenance","acceptance_ids","emitted_evidence_types","supersedes")}})
    @staticmethod
    def _run_id(call_id:str,caller_loop_id:str,definition_id:str)->str:
        return _digest({"schema":SCHEMA,"kind":"SkillRun","call_id":call_id,"caller_loop_id":caller_loop_id,"definition_id":definition_id})
    @classmethod
    def _validate_run(cls,e: Mapping[str,Any])->None:
        for n in ("run_id","caller_state_sha256","source_sha256","definition_id","input_sha256","authority_profile_sha256"): _sha(e.get(n),n)
        for n in ("call_id","caller_loop_id","skill_id","version","capability","authority_profile_id"): _text(e.get(n),n)
        st=SkillRunStatus(e.get("status")); _time(e.get("requested_at"),"requested_at")
        if e.get("completed_at") is not None: _time(e["completed_at"],"completed_at")
        for n in ("available_authorities","available_tools","requested_mutations","used_tools","applied_mutations"): _texts(tuple(e.get(n,[])),n)
        authority_profile = _source_authority_profile(e["authority_profile_id"])
        if e["authority_profile_sha256"] != authority_profile.profile_sha256:
            raise SkillRegistryError("run authority profile digest mismatch")
        if tuple(e["available_authorities"]) != authority_profile.authorities or tuple(e["available_tools"]) != authority_profile.tools:
            raise SkillRegistryError("run authority/tool evidence does not match source-owned profile")
        _prov(tuple(tuple(x) for x in e.get("provenance",[]))); _prov(tuple(tuple(x) for x in e.get("emitted_evidence",[])))
        for n in ("requested_compute_units","requested_data_units","requested_ai_units","consumed_compute_units","consumed_data_units","consumed_ai_units"): _nni(e.get(n),n)
        if type(e.get("input")) is not dict: raise SkillRegistryError("run input must be object")
        if e.get("output") is not None and type(e["output"]) is not dict: raise SkillRegistryError("run output must be object or null")
        if e["input_sha256"] != _digest(e["input"]): raise SkillRegistryError("run input digest mismatch")
        if e["run_id"] != cls._run_id(e["call_id"],e["caller_loop_id"],e["definition_id"]): raise SkillRegistryError("run identity mismatch")
        if st is SkillRunStatus.SUCCEEDED:
            if e["output"] is None or e.get("output_sha256") != _digest(e["output"]): raise SkillRegistryError("successful run output digest mismatch")
            if e.get("error_code") is not None: raise SkillRegistryError("successful run cannot carry error")
        elif e["output"] is not None or e.get("output_sha256") is not None: raise SkillRegistryError("non-successful run cannot carry output")
        q=e.get("research_question_candidate"); qh=e.get("research_question_candidate_sha256")
        if q is None:
            if qh is not None: raise SkillRegistryError("candidate hash lacks candidate")
        else:
            _text(q,"research_question_candidate")
            if st is not SkillRunStatus.SUCCEEDED: raise SkillRegistryError("only successful run can carry candidate")
            if qh != _digest({"kind":"ResearchQuestionCandidate","statement":q,"skill_definition_id":e["definition_id"],"run_id":e["run_id"]}): raise SkillRegistryError("research question candidate digest mismatch")
        if st is SkillRunStatus.RUNNING and e.get("completed_at") is not None: raise SkillRegistryError("running run cannot be completed")
        if st is not SkillRunStatus.RUNNING and e.get("completed_at") is None: raise SkillRegistryError("terminal run requires completed_at")
    def _read(self)->dict[str,Any]:
        try: s=json.loads(self.path.read_text(encoding="utf-8"),object_pairs_hook=_pairs)
        except json.JSONDecodeError as exc: raise SkillRegistryError("SkillRegistry state must be valid JSON") from exc
        if type(s) is not dict or set(s)!={"schema","schema_version","definitions","runs","state_sha256"}: raise SkillRegistryError("SkillRegistry state keys invalid")
        if s["schema"]!=SCHEMA or s["schema_version"]!=SCHEMA_VERSION: raise SkillRegistryError("SkillRegistry schema mismatch")
        if s["state_sha256"]!=_digest(self._without_digest(s)): raise SkillRegistryError("SkillRegistry state digest mismatch")
        keys=set(); ids=set()
        for e in s["definitions"]:
            d=self._definition(e)
            if e.get("definition_id")!=d.definition_id or d.definition_id in ids or d.version_key in keys: raise SkillRegistryError("invalid/duplicate skill definition")
            ids.add(d.definition_id); keys.add(d.version_key)
        runs=set()
        for e in s["runs"]:
            self._validate_run(e)
            if e["run_id"] in runs: raise SkillRegistryError("duplicate durable skill run identity")
            runs.add(e["run_id"])
        return s
    def register(self,d:SkillDefinition)->str:
        if not isinstance(d,SkillDefinition): raise TypeError("definition must be SkillDefinition")
        canonical = _source_executable_definition(d.version_key)
        if canonical is not None and d != canonical:
            raise ConflictingSkillDefinitionError("executable skill definition must exactly match source-owned contract")
        e={"definition_id":d.definition_id,"definition":d.payload()}
        with WorkspaceEconomicLock(self.path.parent):
            s=self._read()
            for old in s["definitions"]:
                od=self._definition(old)
                if od.version_key==d.version_key:
                    if old==e: return d.definition_id
                    raise ConflictingSkillDefinitionError(f"conflicting definition for {d.version_key}")
            s["definitions"].append(e); s["definitions"].sort(key=lambda x:(x["definition"]["skill_id"],x["definition"]["version"])); self._write(self.path,self._without_digest(s))
        return d.definition_id
    def install_builtin_definitions(self)->tuple[str,...]: return tuple(self.register(d) for d in builtin_skill_definitions())
    def resolve(self,*,skill_id:str,version:str,capability:str,definition_id:str)->SkillDefinition:
        skill_id=_text(skill_id,"skill_id"); version=_text(version,"version"); capability=_text(capability,"capability"); definition_id=_sha(definition_id,"definition_id")
        found=[self._definition(e) for e in self._read()["definitions"] if e["definition_id"]==definition_id]
        if len(found)!=1: raise SkillRegistryError("exact skill definition is not registered")
        d=found[0]
        if (d.skill_id,d.version,d.capability)!=(skill_id,version,capability): raise SkillRegistryError("skill identity/capability does not match exact definition")
        canonical = _source_executable_definition(d.version_key)
        if canonical is not None and d != canonical:
            raise SkillPermissionError("registered executable definition is not the source-owned contract")
        return d
    def bind_handler(self,d:SkillDefinition,h:SkillHandler)->None:
        """Reject runtime handler injection.

        Executable handlers are source-owned entries in _BUILTIN_HANDLERS. A new
        reviewed plugin therefore becomes executable only by changing reviewed
        source, tests, and the normal PR lineage; registering metadata alone never
        turns an arbitrary callable into production code.
        """
        if not isinstance(d,SkillDefinition): raise TypeError("definition must be SkillDefinition")
        if not callable(h): raise TypeError("handler must be callable")
        raise SkillPermissionError("runtime handler binding is forbidden; executable handlers must be source-reviewed")
    def _run(self,e)->SkillRun:
        return SkillRun(**{**{k:e[k] for k in ("run_id","call_id","caller_loop_id","caller_state_sha256","source_sha256","definition_id","skill_id","version","capability","authority_profile_id","authority_profile_sha256")},
            "status":SkillRunStatus(e["status"]),"requested_at":e["requested_at"],"completed_at":e["completed_at"],"input_sha256":e["input_sha256"],"output_sha256":e["output_sha256"],
            **{n:tuple(e[n]) for n in ("available_authorities","available_tools","requested_mutations","used_tools","applied_mutations")},
            "provenance":tuple(tuple(x) for x in e["provenance"]),"emitted_evidence":tuple(tuple(x) for x in e["emitted_evidence"]),
            **{n:e[n] for n in ("requested_compute_units","requested_data_units","requested_ai_units","consumed_compute_units","consumed_data_units","consumed_ai_units","research_question_candidate_sha256","research_question_candidate","error_code")},
            "output":None if e["output"] is None else dict(e["output"])})
    def get_run(self,run_id:str)->SkillRun:
        run_id=_sha(run_id,"run_id"); found=[e for e in self._read()["runs"] if e["run_id"]==run_id]
        if len(found)!=1: raise SkillRegistryError("skill run not found")
        return self._run(found[0])
    def recover_incomplete(self,*,at:str)->tuple[str,...]:
        now=_time(at,"at"); changed=[]
        with WorkspaceEconomicLock(self.path.parent):
            s=self._read()
            for e in s["runs"]:
                if e["status"]==SkillRunStatus.RUNNING.value: e.update(status=SkillRunStatus.INTERRUPTED.value,completed_at=now,error_code="RESTART_INTERRUPTED"); changed.append(e["run_id"])
            if changed: self._write(self.path,self._without_digest(s))
        return tuple(changed)
    @staticmethod
    def _denial(d,a,t,m,p,c,db,ai)->str|None:
        if not set(d.required_authorities).issubset(a): return "MISSING_REQUIRED_AUTHORITY"
        if not set(d.required_tools).issubset(t): return "MISSING_REQUIRED_TOOL"
        if set(m)&NON_DELEGABLE_MUTATIONS: return "NON_DELEGABLE_MUTATION"
        if not set(m).issubset(d.allowed_mutations): return "MUTATION_NOT_ALLOWED"
        if set(m)&set(d.forbidden_mutations): return "FORBIDDEN_MUTATION"
        if not set(d.required_provenance).issubset({k for k,_ in p}): return "MISSING_REQUIRED_PROVENANCE"
        if c>d.compute_budget_units:return "COMPUTE_BUDGET_EXCEEDED"
        if db>d.data_budget_units:return "DATA_BUDGET_EXCEEDED"
        if ai>d.ai_budget_units:return "AI_BUDGET_EXCEEDED"
        return None
    @staticmethod
    def _check_result(d,r):
        if set(r.used_tools)-set(d.required_tools): raise SkillPermissionError("handler used undeclared tool")
        if set(r.applied_mutations)&NON_DELEGABLE_MUTATIONS: raise SkillPermissionError("handler reported protected mutation")
        if not set(r.applied_mutations).issubset(d.allowed_mutations): raise SkillPermissionError("handler reported undeclared mutation")
        if r.consumed_compute_units>d.compute_budget_units or r.consumed_data_units>d.data_budget_units or r.consumed_ai_units>d.ai_budget_units: raise SkillPermissionError("handler exceeded budget")
        if {k for k,_ in r.emitted_evidence}-set(d.emitted_evidence_types): raise SkillPermissionError("handler emitted undeclared evidence type")
    def invoke(self,*,skill_id:str,version:str,capability:str,definition_id:str,call_id:str,caller_loop_id:str,caller_state_sha256:str,source_sha256:str,input_payload:dict[str,Any],authority_profile_id:str,requested_mutations:tuple[str,...],provenance:tuple[tuple[str,str],...],requested_compute_units:int,requested_data_units:int,requested_ai_units:int,at:str)->SkillRun:
        d=self.resolve(skill_id=skill_id,version=version,capability=capability,definition_id=definition_id)
        if d.implementation_kind is SkillImplementationKind.CANDIDATE_DYNAMIC_CODE: raise SkillPermissionError("candidate dynamic code is not executable")
        authority_profile=_source_authority_profile(authority_profile_id)
        call_id=_text(call_id,"call_id"); caller_loop_id=_text(caller_loop_id,"caller_loop_id"); caller_state_sha256=_sha(caller_state_sha256,"caller_state_sha256"); source_sha256=_sha(source_sha256,"source_sha256")
        payload=_obj(input_payload,"input_payload"); a=authority_profile.authorities; t=authority_profile.tools; m=_texts(requested_mutations,"requested_mutations"); p=_prov(provenance)
        c=_pos(requested_compute_units,"requested_compute_units"); db=_nni(requested_data_units,"requested_data_units"); ai=_nni(requested_ai_units,"requested_ai_units"); now=_time(at,"at")
        rid=self._run_id(call_id,caller_loop_id,d.definition_id); inp=_digest(payload)
        immutable={"call_id":call_id,"caller_loop_id":caller_loop_id,"caller_state_sha256":caller_state_sha256,"source_sha256":source_sha256,"definition_id":d.definition_id,"skill_id":d.skill_id,"version":d.version,"capability":d.capability,"input_sha256":inp,"input":payload,"authority_profile_id":authority_profile.profile_id,"authority_profile_sha256":authority_profile.profile_sha256,"available_authorities":list(a),"available_tools":list(t),"requested_mutations":list(m),"provenance":[list(x) for x in p],"requested_compute_units":c,"requested_data_units":db,"requested_ai_units":ai}
        with WorkspaceEconomicLock(self.path.parent):
            s=self._read(); old=next((x for x in s["runs"] if x["run_id"]==rid),None)
            if old is not None:
                if any(old.get(k)!=v for k,v in immutable.items()): raise ConflictingSkillRunError("skill invocation retry changes immutable request")
                run=self._run(old)
                if run.status in {SkillRunStatus.RUNNING,SkillRunStatus.INTERRUPTED}: raise SkillRecoveryRequiredError("prior invocation is in-flight/interrupted; blind replay is forbidden")
                return run
            denial=self._denial(d,a,t,m,p,c,db,ai)
            e={"run_id":rid,**immutable,"requested_at":now,"completed_at":now if denial else None,"output_sha256":None,"output":None,"consumed_compute_units":0,"consumed_data_units":0,"consumed_ai_units":0,"used_tools":[],"applied_mutations":[],"emitted_evidence":[],"research_question_candidate_sha256":None,"research_question_candidate":None,"error_code":denial,"status":SkillRunStatus.DENIED.value if denial else SkillRunStatus.RUNNING.value}
            s["runs"].append(e); self._write(self.path,self._without_digest(s))
            if denial:return self._run(e)
        h=self._handlers.get(d.version_key)
        if h is None:return self._finish_failure(rid,now,"HANDLER_UNAVAILABLE")
        r,error=self._execute_handler_bounded(h,payload,d.timeout_seconds)
        if error is not None:return self._finish_failure(rid,now,error)
        try:
            if not isinstance(r,SkillExecutionResult):raise SkillRegistryError("skill handler must return SkillExecutionResult")
            self._check_result(d,r)
        except Exception as exc:return self._finish_failure(rid,now,"HANDLER_ERROR_"+exc.__class__.__name__.upper())
        return self._finish_success(rid,d,r,now)
    @staticmethod
    def _execute_handler_bounded(h:SkillHandler,payload:dict[str,Any],timeout_seconds:int)->tuple[SkillExecutionResult|None,str|None]:
        context=multiprocessing.get_context("spawn")
        receiver,sender=context.Pipe(duplex=False)
        process=context.Process(target=_skill_handler_process,args=(h,payload,sender),daemon=True)
        try:
            process.start()
        except Exception as exc:
            receiver.close(); sender.close()
            return None,"HANDLER_START_"+exc.__class__.__name__.upper()
        sender.close()
        process.join(timeout_seconds)
        if process.is_alive():
            process.terminate(); process.join(2)
            if process.is_alive() and hasattr(process,"kill"):
                process.kill(); process.join(2)
            receiver.close()
            return None,"HANDLER_TIMEOUT"
        if not receiver.poll():
            receiver.close()
            return None,"HANDLER_PROCESS_EXITED"
        try:
            kind,value=receiver.recv()
        except (EOFError,OSError):
            return None,"HANDLER_RESULT_UNAVAILABLE"
        finally:
            receiver.close()
        if kind=="ERROR":
            return None,"HANDLER_ERROR_"+_text(value,"handler error type").upper()
        if kind!="OK":
            return None,"HANDLER_PROTOCOL_ERROR"
        return value,None
    def _finish_failure(self,rid,at,code):
        with WorkspaceEconomicLock(self.path.parent):
            s=self._read(); e=next(x for x in s["runs"] if x["run_id"]==rid)
            if e["status"]==SkillRunStatus.RUNNING.value:e.update(status=SkillRunStatus.FAILED.value,completed_at=at,error_code=_text(code,"error_code")); self._write(self.path,self._without_digest(s))
            return self._run(e)
    def _finish_success(self,rid,d,r,at):
        out=_obj(r.output,"result.output"); q=r.research_question_candidate; qh=None if q is None else _digest({"kind":"ResearchQuestionCandidate","statement":q,"skill_definition_id":d.definition_id,"run_id":rid})
        with WorkspaceEconomicLock(self.path.parent):
            s=self._read(); e=next(x for x in s["runs"] if x["run_id"]==rid)
            if e["status"]==SkillRunStatus.RUNNING.value:
                e.update(status=SkillRunStatus.SUCCEEDED.value,completed_at=at,output_sha256=_digest(out),output=out,consumed_compute_units=r.consumed_compute_units,consumed_data_units=r.consumed_data_units,consumed_ai_units=r.consumed_ai_units,used_tools=list(r.used_tools),applied_mutations=list(r.applied_mutations),emitted_evidence=[list(x) for x in r.emitted_evidence],research_question_candidate_sha256=qh,research_question_candidate=q,error_code=None); self._write(self.path,self._without_digest(s))
            return self._run(e)

def _provider_gap(p):
    provider=_text(p.get("provider_id"),"provider_id"); as_of=_time(p.get("as_of"),"as_of"); expected=p.get("expected_fields"); observed=p.get("observed_fields")
    if type(expected) is not list or type(observed) is not list: raise SkillRegistryError("expected_fields/observed_fields must be lists")
    expected=tuple(sorted({_text(x,"expected field") for x in expected})); observed=tuple(sorted({_text(x,"observed field") for x in observed})); missing=sorted(set(expected)-set(observed)); unexpected=sorted(set(observed)-set(expected))
    out={"provider_id":provider,"as_of":as_of,"status":"GAP" if missing else "COMPLETE_FOR_DECLARED_FIELDS","missing_fields":missing,"unexpected_fields":unexpected}
    return SkillExecutionResult(out,(("PROVIDER_GAP_DIAGNOSTIC",_digest(out)),),consumed_compute_units=1)

def _drift(p):
    raw=p.get("findings")
    if type(raw) is not list: raise SkillRegistryError("findings must be a list")
    rows=[]
    for x in raw:
        if type(x) is not dict or type(x.get("triggered")) is not bool: raise SkillRegistryError("invalid drift finding")
        rows.append({"metric":_text(x.get("metric"),"metric"),"triggered":x["triggered"],"evidence_sha256":_sha(x.get("evidence_sha256"),"evidence_sha256")})
    rows.sort(key=lambda x:x["metric"]); out={"scope":_text(p.get("scope"),"scope"),"triggered_metrics":[x["metric"] for x in rows if x["triggered"]],"findings":rows,"truth":"DIAGNOSTIC_ONLY_NOT_GLOBAL_MODEL_STABILITY"}
    return SkillExecutionResult(out,(("DRIFT_DIAGNOSTIC",_digest(out)),),consumed_compute_units=1)

def _postmortem(p):
    u=p.get("unresolved_codes"); ev=p.get("evidence_sha256")
    if type(u) is not list or type(ev) is not list: raise SkillRegistryError("unresolved_codes/evidence_sha256 must be lists")
    u=tuple(sorted({_text(x,"unresolved code") for x in u})); ev=tuple(sorted({_sha(x,"evidence_sha256") for x in ev})); q=p.get("research_question_candidate")
    if q is not None:
        q=_text(q,"research_question_candidate")
        if not u: raise SkillRegistryError("research question candidate requires unresolved evidence")
    out={"subject_id":_text(p.get("subject_id"),"subject_id"),"unresolved_codes":list(u),"evidence_sha256":list(ev),"status":"UNRESOLVED" if u else "NO_UNRESOLVED_FINDING","research_question_authority":"CANDIDATE_ONLY_CANONICAL_HANDOFF_REQUIRED"}
    return SkillExecutionResult(out,(("POSTMORTEM_DIAGNOSTIC",_digest(out)),),consumed_compute_units=1,research_question_candidate=q)

_BUILTIN_HANDLERS: Final[dict[str,SkillHandler]]={"provider-gap@1.0.0":_provider_gap,"drift-inspection@1.0.0":_drift,"postmortem@1.0.0":_postmortem}
def _source_handler_hash(handler: SkillHandler) -> str:
    return _builtin_hash("handler:"+handler.__module__+":"+handler.__qualname__)
def builtin_skill_definitions()->tuple[SkillDefinition,...]:
    schema=lambda n:_builtin_hash("schema:"+n); common={"required_authorities":("READ_ONLY_ANALYSIS",),"required_provenance":("source_evidence",),"timeout_seconds":10,"compute_budget_units":1}
    return (
        SkillDefinition("provider-gap","1.0.0","diagnose_provider_gap","Compare declared provider fields without inventing missing evidence.",schema("provider-gap-input-v1"),schema("provider-gap-output-v1"),_source_handler_hash(_BUILTIN_HANDLERS["provider-gap@1.0.0"]),SkillImplementationKind.BUILTIN,acceptance_ids=("AS-547-PROVIDER-GAP",),emitted_evidence_types=("PROVIDER_GAP_DIAGNOSTIC",),**common),
        SkillDefinition("drift-inspection","1.0.0","inspect_drift","Aggregate evidenced drift findings without promotion authority.",schema("drift-input-v1"),schema("drift-output-v1"),_source_handler_hash(_BUILTIN_HANDLERS["drift-inspection@1.0.0"]),SkillImplementationKind.BUILTIN,acceptance_ids=("AS-547-DRIFT",),emitted_evidence_types=("DRIFT_DIAGNOSTIC",),**common),
        SkillDefinition("postmortem","1.0.0","produce_postmortem","Structure unresolved evidence and emit only a research-question candidate.",schema("postmortem-input-v1"),schema("postmortem-output-v1"),_source_handler_hash(_BUILTIN_HANDLERS["postmortem@1.0.0"]),SkillImplementationKind.BUILTIN,acceptance_ids=("AS-547-POSTMORTEM",),emitted_evidence_types=("POSTMORTEM_DIAGNOSTIC",),**common),
    )
def _source_executable_definition(version_key: str) -> SkillDefinition | None:
    if version_key not in _BUILTIN_HANDLERS:
        return None
    return next(d for d in builtin_skill_definitions() if d.version_key == version_key)
