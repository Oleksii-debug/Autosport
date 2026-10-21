"""Deterministic, non-authorizing Windows update transaction preview.

This is the bridge from signed preflight evidence to an operator-readable
migration/backup plan.  It does NOT grant filesystem mutation or installation
capability. A future product-owned installer must independently revalidate
package bytes, installed trust and backup/recovery state inside its transaction.
"""
from __future__ import annotations

from .windows_update_admission import _schema, _version
from .windows_update_signature import VerifiedWindowsUpdate


class WindowsUpdatePlanError(ValueError):
    """The proposed transaction cannot be represented safely."""


def preview_windows_update_transaction(
    verified: VerifiedWindowsUpdate,
    *,
    supported_schema_migrations: tuple[tuple[int, int], ...],
) -> dict[str, object]:
    """Return immutable-fact JSON primitives; never return an installer token.

    The migration chain is the caller's locally supported chain, not an
    instruction supplied by an untrusted ZIP. Reject noncanonical or forged
    receipts even when the preflight backend is mocked or compromised.
    """
    try:
        if type(verified) is not VerifiedWindowsUpdate:
            raise WindowsUpdatePlanError("signed preflight receipt required")
        verified.__post_init__()
        decision = verified.decision
        decision.__post_init__()
        if (
            verified.install_authorized is not False
            or decision.install_authorized is not False
            or type(supported_schema_migrations) is not tuple
        ):
            raise WindowsUpdatePlanError("update preview cannot grant installation")
        old = _version(decision.from_version)
        new = _version(decision.to_version)
        old_schema = _schema(decision.from_schema)
        new_schema = _schema(decision.to_schema)
        if verified.version != decision.to_version or verified.schema != new_schema:
            raise WindowsUpdatePlanError("candidate receipt conflicts with migration plan")
        if decision.disposition == "NO_CHANGE":
            if new != old or new_schema != old_schema or supported_schema_migrations:
                raise WindowsUpdatePlanError("no-change decision has update effects")
            steps: list[dict[str, int]] = []
            required = False
        elif decision.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE":
            if new <= old or new_schema < old_schema:
                raise WindowsUpdatePlanError("update is a downgrade or non-progressing")
            count = new_schema - old_schema
            if count > 256 or len(supported_schema_migrations) != count:
                raise WindowsUpdatePlanError("unsupported migration chain")
            steps = []
            for offset, edge in enumerate(supported_schema_migrations):
                start = old_schema + offset
                if (
                    type(edge) is not tuple or len(edge) != 2
                    or type(edge[0]) is not int or type(edge[1]) is not int
                    or edge != (start, start + 1)
                ):
                    raise WindowsUpdatePlanError("noncanonical migration edge")
                steps.append({"from_schema": start, "to_schema": start + 1})
            required = True
        else:
            raise WindowsUpdatePlanError("unknown update disposition")
        return {
            "status": "PREVIEW_ONLY_UNTRUSTED_OPERATOR_ROOT",
            "disposition": decision.disposition,
            "from_version": decision.from_version,
            "to_version": decision.to_version,
            "from_schema": old_schema,
            "to_schema": new_schema,
            "package_sha256": verified.package_sha256,
            "source_sha": verified.source_sha,
            "backup_required": required,
            "migration_steps": steps,
            "restart_recovery_required": required,
            "independent_installer_trust_required": True,
            "install_authorized": False,
        }
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise WindowsUpdatePlanError("Windows update transaction preview rejected") from None
