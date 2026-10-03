"""Seal PAPER product-day chronology issuance after preload composition.

The raw permit issuer, permit consumer, causal-history advance helper, and loaded-state
causal installer are composition capabilities rather than runtime APIs. Keep canonical
PaperBook transitions self-contained in closure-owned state, then remove those handles
from normal module access after guarded load/save have captured the durable graph.
"""

from __future__ import annotations

from types import FunctionType

from . import _paperbook_preload_authority_guard as _preload_guard
from . import paper as _paper


def _install() -> None:
    paper_book = _paper.PaperBook
    class_namespace = vars(paper_book)
    raw_prepare = class_namespace.get("_prepare_product_day_admission")
    raw_record = class_namespace.get("_record_product_day_admission")
    issue_permit = _paper.__dict__.get("_issue_product_day_admission_permit")
    consume_permit = _paper.__dict__.get("_consume_product_day_admission_permit")
    causal_advance = _paper.__dict__.get(
        "_advance_paperbook_product_day_admission"
    )
    require_opening = _paper.__dict__.get("_require_ticket_opening_authority")
    require_causal = _paper.__dict__.get(
        "_require_paperbook_causal_history_authority"
    )
    if any(
        type(value) is not FunctionType
        for value in (
            raw_prepare,
            raw_record,
            issue_permit,
            consume_permit,
            causal_advance,
            require_opening,
            require_causal,
        )
    ):
        raise RuntimeError(
            "canonical PaperBook product-day transition authority is unavailable"
        )

    def prepare_product_day_admission(
        self,
        *,
        admission_ts: str,
        window_store: object,
        window_evidence: object,
        workspace_lock: object,
    ) -> object:
        witness = raw_prepare(
            self,
            admission_ts=admission_ts,
            window_store=window_store,
            window_evidence=window_evidence,
            workspace_lock=workspace_lock,
        )
        return issue_permit(self, witness)

    def record_product_day_admission(
        self,
        ticket_id: str,
        *,
        permit: object,
    ) -> None:
        require_opening(self)
        require_causal(self)
        type(self)._validate_loaded_state(self)
        ticket = self.tickets.get(ticket_id)
        if ticket is None:
            raise ValueError(
                "PaperBook product-day admission references unknown ticket"
            )
        if ticket_id in self._product_day_admissions:
            raise ValueError(
                "PaperBook product-day admission authority cannot be rebound"
            )
        witness = consume_permit(
            self,
            permit,
            placed_at=ticket.placed_at,
        )
        witness = self._validate_product_day_admission_witness(
            witness,
            ticket_id=ticket_id,
        )
        self._product_day_admissions[ticket_id] = witness
        try:
            causal_advance(
                self,
                ticket_id,
                witness,
            )
        except Exception:
            self._product_day_admissions.pop(ticket_id, None)
            raise

    prepare_product_day_admission.__name__ = "_prepare_product_day_admission"
    prepare_product_day_admission.__qualname__ = (
        "PaperBook._prepare_product_day_admission"
    )
    prepare_product_day_admission.__doc__ = (
        "Issue one closure-owned current-day chronology permit before mutation."
    )
    record_product_day_admission.__name__ = "_record_product_day_admission"
    record_product_day_admission.__qualname__ = (
        "PaperBook._record_product_day_admission"
    )
    record_product_day_admission.__doc__ = raw_record.__doc__

    paper_book._prepare_product_day_admission = prepare_product_day_admission
    paper_book._record_product_day_admission = record_product_day_admission

    # Guarded load/save are detached before this module imports. These composition
    # capabilities therefore must not remain callable through normal module access.
    for name in (
        "_issue_product_day_admission_permit",
        "_consume_product_day_admission_permit",
        "_advance_paperbook_product_day_admission",
        "_install_validated_paperbook_causal_history_authority",
    ):
        value = _paper.__dict__.get(name)
        if type(value) is not FunctionType:
            raise RuntimeError(
                f"obsolete PaperBook product-day capability is unavailable: {name}"
            )
        del _paper.__dict__[name]

    install_causal = _preload_guard.__dict__.get("_INSTALL_CAUSAL")
    if type(install_causal) is not FunctionType:
        raise RuntimeError(
            "obsolete PaperBook preload causal installer is unavailable"
        )
    del _preload_guard.__dict__["_INSTALL_CAUSAL"]


_install()
del _install
del FunctionType
del _paper
del _preload_guard
