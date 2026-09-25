"""Verifier: independent pre-finalization checks. It never calls MCP and never edits output.

Besides schema and cross-field invariants, the verifier independently re-derives two key facts
from raw case evidence (not from specialist results): the case-scoped order record and the
captured total inside the case window. It returns ``{"valid", "errors", "warnings"}``; the
coordinator decides whether to finalize or run its single bounded repair pass.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..agent_types import VERIFIER, AgentResult, AgentTask, Status
from ..evidence_cache import CaseEvidenceStore
from ..money import TOLERANCE, to_decimal
from ..scoping import PURCHASE, select_scoped_record
from .payment_refund import capture_groups

REFUND_WORDS = ("refund", "reimburse")


def _money(value: Any) -> Decimal | None:
    return to_decimal(value)


def check_invariants(output: dict[str, Any], case: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if output.get("case_id") != case.get("case_id"):
        errors.append("case_id_mismatch")

    entity = output["entity_resolution"]
    resolved, rejected = set(entity["resolved_order_ids"]), set(entity["rejected_candidates"])
    if resolved & rejected:
        errors.append("resolved_and_rejected_overlap")
    if not resolved <= set(output["affected_entities"]["order_ids"]):
        errors.append("resolved_order_not_affected")
    if entity["status"] == "resolved" and not resolved:
        errors.append("resolved_without_order")
    if entity["status"] != "resolved" and entity["confidence"] > 0.6:
        warnings.append("unresolved_entity_high_confidence")

    refs = output["evidence_refs"]
    if len(refs) != len(set(refs)):
        errors.append("duplicate_evidence_refs")

    shipment = output["shipment_analysis"]
    sellers = set(output["affected_entities"]["seller_ids"])
    if not set(shipment["late_seller_ids"]) <= sellers:
        errors.append("late_seller_not_affected")
    if shipment["verdict"] == "seller_delay" and not shipment["late_seller_ids"]:
        errors.append("seller_delay_without_late_seller")
    if shipment["verdict"] != "seller_delay" and shipment["late_seller_ids"]:
        errors.append("late_sellers_without_seller_delay")

    payment = output["payment_analysis"]
    captured = _money(payment["captured_total_brl"])
    refunded = _money(payment["refunded_total_brl"])
    refundable = _money(payment["refundable_total_brl"])
    for name in ("captured_total_brl", "refunded_total_brl", "refundable_total_brl"):
        value = _money(payment[name])
        if value is not None and value < 0:
            errors.append(f"negative_{name}")
    known = captured is not None and refunded is not None and refundable is not None
    if known and abs(refundable - max(captured - refunded, Decimal(0))) > TOLERANCE:
        errors.append("refundable_total_inconsistent")

    financial = output["financial_resolution"]
    recommended = _money(financial["recommended_refund_brl"]) or Decimal(0)
    lines = sum(
        (_money(line["amount_brl"]) or Decimal(0) for line in financial["refund_lines"]), Decimal(0)
    )
    if abs(lines - recommended) > TOLERANCE:
        errors.append("refund_lines_do_not_sum")
    if refundable is not None and recommended > refundable + TOLERANCE:
        errors.append("refund_exceeds_refundable")
    if payment["verdict"] == "refunded" and recommended > 0:
        errors.append("refund_after_completed_refund")

    assessment = output["assessment"]
    actions = output["resolution_actions"]
    if len(actions) != len(set(actions)):
        errors.append("duplicate_actions")
    if assessment["case_status"] == "no_action":
        if recommended > 0:
            errors.append("no_action_with_refund")
        if any(word in action for action in actions for word in REFUND_WORDS):
            errors.append("no_action_with_refund_action")
    if assessment["case_status"] == "action_required" and not actions:
        errors.append("action_required_without_action")

    for party in output["root_cause_analysis"]["responsible_parties"]:
        if (
            party["party_type"] == "seller"
            and party["party_id"]
            and party["party_id"] not in sellers
        ):
            errors.append("responsible_seller_not_affected")
    issue = assessment["primary_issue"]
    if issue == "late_delivery_seller" and shipment["verdict"] != "seller_delay":
        errors.append("issue_shipment_verdict_mismatch")
    if issue == "late_delivery_logistics" and shipment["verdict"] != "logistics_delay":
        errors.append("issue_shipment_verdict_mismatch")
    if not 0 <= assessment["confidence"] <= 1:
        errors.append("confidence_out_of_range")
    if issue == "insufficient_evidence" and assessment["confidence"] > 0.5:
        warnings.append("insufficient_evidence_high_confidence")
    return errors, warnings


def independent_checks(
    output: dict[str, Any], case: dict[str, Any], store: CaseEvidenceStore, scoped_purchase: Any
) -> list[str]:
    errors: list[str] = []
    resolved = output["entity_resolution"]["resolved_order_ids"]
    if not resolved:
        return errors
    order_id = resolved[0]
    rows: list[dict[str, Any]] = []
    for evidence in store.by_tool("get_customer_history"):
        if isinstance(evidence.data, dict):
            rows += [
                row for row in evidence.data.get("orders") or [] if row.get("order_id") == order_id
            ]
    for evidence in store.by_tool("get_order"):
        if isinstance(evidence.data, dict) and evidence.data.get("order_id") == order_id:
            rows.append(evidence.data)
    scoped = select_scoped_record(rows, case.get("opened_at"))
    if scoped is None or scoped.row.get(PURCHASE) != scoped_purchase:
        errors.append("scoped_record_not_reproducible")
        return errors
    totals: list[Decimal] = []
    for evidence in store.by_tool("get_payment_timeline"):
        if not isinstance(evidence.data, dict):
            continue
        captures = [
            event
            for event in evidence.data.get("events") or []
            if str(event.get("event_type")).lower() == "captured"
            and str(event.get("status", "confirmed")).lower() == "confirmed"
        ]
        payments = [row for row in evidence.data.get("payments") or [] if isinstance(row, dict)]
        in_window = [event for event in captures if scoped.window.contains(event.get("event_at"))]
        if not in_window:
            continue
        totals.append(
            sum((to_decimal(e.get("amount_brl")) or Decimal(0) for e in in_window), Decimal(0))
        )
        if len(payments) == len(captures):
            pairs = [(p, e) for p, e in zip(payments, captures, strict=True) if e in in_window]
            for group in capture_groups(pairs):
                totals.append(
                    sum(
                        (to_decimal(e.get("amount_brl")) or Decimal(0) for _, e in group),
                        Decimal(0),
                    )
                )
    reported = _money(output["payment_analysis"]["captured_total_brl"])
    if totals and (reported is None or all(abs(reported - t) > TOLERANCE for t in totals)):
        errors.append("captured_total_not_reproducible")
    return errors


def unconsumed_refs(output: dict[str, Any], store: CaseEvidenceStore) -> list[str]:
    consumed = set(store.consumed_refs())
    refs = list(output["evidence_refs"])
    for claim in output.get("claim_assessments", []):
        refs += claim["evidence_refs"]
    return [ref for ref in refs if ref not in consumed or not store.owns(ref)]


async def run_verifier(task: AgentTask, store: CaseEvidenceStore) -> AgentResult:
    output: dict[str, Any] = task.payload["output"]
    case: dict[str, Any] = task.payload["case"]
    contracts = task.payload.get("contracts")
    errors: list[str] = []
    warnings: list[str] = []
    if contracts is not None:
        try:
            contracts.validate_output(output, "draft output")
        except ValueError as exc:
            errors.append(f"schema:{str(exc)[:120]}")
    if not errors:
        more_errors, warnings = check_invariants(output, case)
        errors += more_errors
        errors += independent_checks(output, case, store, task.payload.get("scoped_purchase"))
        if unconsumed_refs(output, store):
            errors.append("evidence_not_consumed_in_case")
    valid = not errors
    decision = "VERIFY_PASS" if valid else "VERIFY_FAIL"
    store.trace.emit(
        case_id=task.case_id,
        event_type="verification_completed",
        actor=VERIFIER,
        target="coordinator",
        decision_code=decision,
        attributes={"error_count": len(errors), "warning_count": len(warnings)},
    )
    return AgentResult.reply(
        task,
        status=Status.OK if valid else Status.NEEDS_FOLLOWUP,
        confidence=0.95 if valid else 0.3,
        decision_code=decision,
        warnings=warnings,
        data={"valid": valid, "errors": errors, "warnings": warnings},
    )
