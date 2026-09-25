from __future__ import annotations

import inspect
import math
import re
from collections.abc import Mapping
from typing import Any

from .conflict_resolver import (
    PRIMARY_ISSUES,
    build_financial_resolution,
    claim_topics,
    dedupe,
    derive_conflicts,
    derive_root_cause,
    policy_is_needed,
    primary_issue_for,
    refund_allowed,
)
from .mcp_gateway import EvidenceGateway
from .state import (
    EntityResult,
    PaymentResult,
    PolicyResult,
    ShipmentResult,
)
from .trace import TraceWriter

_ACTOR = "policy_agent"


async def _available_tools(gateway: EvidenceGateway) -> list[str] | None:
    method = getattr(gateway, "list_tools", None)
    if method is None:
        return None
    try:
        result = method()
        if inspect.isawaitable(result):
            result = await result
    except Exception:  # noqa: BLE001 - discovery failure is missing policy evidence
        return []
    return [str(name) for name in (result or [])]


def _choose_tool(available: list[str] | None, preferred: str) -> str | None:
    if available is None:
        return preferred
    return preferred if preferred in available else None


def _money(value: Any) -> float | None:
    if value is None or isinstance(value, bool | Mapping | list | tuple):
        return None
    text = str(value).replace("R$", "").replace("BRL", "").strip()
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    text = re.sub(r"[^0-9.\-]", "", text)
    try:
        amount = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount) or amount < 0:
        return None
    return round(amount, 2)


def _policy_money(data: Any) -> float | None:
    keys = (
        "max_refund_brl",
        "maximum_refund_brl",
        "refund_limit_brl",
        "allowed_refund_brl",
        "refundable_total_brl",
    )
    for mapping in walk_mappings(data):
        value = first_value(mapping, keys)
        amount = _money(value)
        if amount is not None:
            return amount
    return None


def _policy_allows_refund(data: Any) -> bool | None:
    values = collect_bools(data, ("refund_allowed", "refund_eligible", "eligible_for_refund"))
    if not values:
        return None
    return all(values)


def _claim_topics(case: Mapping[str, Any]) -> list[str]:
    request = case.get("customer_request")
    if not isinstance(request, Mapping):
        return []
    claims = request.get("claims")
    if not isinstance(claims, list):
        return []
    result: list[str] = []
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        topic = claim.get("topic") or claim.get("claim_type")
        if isinstance(topic, str) and topic.strip():
            value = normalise_field(topic)
            if value not in result:
                result.append(value)
    return result


def _trace_evidence(
    trace: TraceWriter,
    case_id: str,
    tool_name: str,
    evidence: Any,
) -> list[str]:
    reference = evidence_ref(evidence)
    if reference is None:
        return []
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=_ACTOR,
        tool_name=tool_name,
        evidence_refs=[reference],
    )
    return [reference]


def _payment_issue(payment: PaymentResult) -> str | None:
    return {
        "capture_mismatch": "payment_mismatch",
        "duplicate_capture": "duplicate_charge",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }.get(payment.verdict)


def _shipment_issue(shipment: ShipmentResult) -> str | None:
    return {
        "seller_delay": "late_delivery_seller",
        "logistics_delay": "late_delivery_logistics",
    }.get(shipment.verdict)


def _cause_and_party(
    issue: str,
    shipment: ShipmentResult,
) -> tuple[RankedCause, list[ResponsibleParty]]:
    if issue == "late_delivery_seller":
        parties = [
            ResponsibleParty("seller", seller_id)
            for seller_id in shipment.late_seller_ids
        ] or [ResponsibleParty("seller", None)]
        return RankedCause("SELLER_BREACH", 1), parties
    if issue == "late_delivery_logistics":
        return RankedCause("LOGISTICS_DELAY", 1), [ResponsibleParty("logistics_provider", None)]
    if issue in {"payment_mismatch", "duplicate_charge"}:
        cause = "DUPLICATE_CHARGE" if issue == "duplicate_charge" else "PAYMENT_MISMATCH"
        return RankedCause(cause, 1), [ResponsibleParty("payment_provider", None)]
    if issue in {"refund_pending", "refund_failed"}:
        cause = "REFUND_PROCESSING" if issue == "refund_pending" else "REFUND_FAILURE"
        return RankedCause(cause, 1), [ResponsibleParty("payment_provider", None)]
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        return RankedCause("ORDER_FULFILLMENT", 1), [ResponsibleParty("platform", None)]
    return RankedCause("INSUFFICIENT_EVIDENCE", 1), [ResponsibleParty("unknown", None)]


class PolicyAgent:
    """Apply the public policy evidence to the specialist-agent results."""

    async def resolve_conflict(
        self,
        case: dict[str, Any],
        entity: EntityResult,
        shipment: ShipmentResult,
        payment: PaymentResult,
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> PolicyResult:
        """
        Nhiệm vụ: Đọc policy và phân xử đúng sai dựa trên evidence.

        Input:
          - case: dữ liệu gốc (policy_version, claims...)
          - entity: kết quả từ EntityAgent
          - shipment: kết quả từ OrderShipmentAgent
          - payment: kết quả từ PaymentAgent

        Gọi MCP tools (ví dụ): get_policy, search_policy...
        Ghi trace: tool_result_consumed, policy_decided
        """
        case_id = case["case_id"]
        topics = claim_topics(case)
        policy_data: Any = None
        policy_evidence_refs: list[str] = []
        policy_tool_name: str | None = None

        if policy_is_needed(topics):
            policy_tool_name = await self._discover_policy_tool(gateway)
            if policy_tool_name is not None:
                try:
                    evidence = await gateway.call(
                        policy_tool_name,
                        case_id=case_id,
                        policy_version=str(case.get("policy_version", "")),
                    )
                except (RuntimeError, ValueError, KeyError):
                    evidence = None
                if evidence is not None:
                    policy_data = evidence.get("data")
                    evidence_ref = evidence.get("evidence_ref")
                    if isinstance(evidence_ref, str):
                        policy_evidence_refs.append(evidence_ref)
                        trace.emit(
                            case_id=case_id,
                            event_type="tool_result_consumed",
                            actor="policy-agent",
                            tool_name=policy_tool_name,
                            evidence_refs=policy_evidence_refs,
                        )

        conflicts = derive_conflicts(case, shipment, payment)
        all_evidence_refs = dedupe(
            entity.evidence_refs
            + shipment.evidence_refs
            + payment.evidence_refs
            + policy_evidence_refs
        )
        unresolved_conflict = any(conflict.selected_source is None for conflict in conflicts)
        policy_evidence_loaded = bool(policy_evidence_refs)
        refund_policy = refund_allowed(policy_data)
        policy_supports_refund = refund_policy is True
        policy_forbids_refund = refund_policy is False
        unsupported_claim = self._is_unsupported_claim(
            topics,
            shipment,
            payment,
            policy_forbids_refund=policy_forbids_refund,
        )
        primary_issue = primary_issue_for(
            shipment,
            payment,
            topics,
            unsupported_claim=unsupported_claim,
        )
        if "requested_full_refund" in topics and policy_forbids_refund:
            primary_issue = "unsupported_claim"
        elif "requested_full_refund" in topics and policy_supports_refund:
            primary_issue = self._refund_issue(payment, primary_issue)
        ranked_causes, responsible_parties = derive_root_cause(
            shipment,
            payment,
            topics,
            unsupported_claim=unsupported_claim,
        )
        financial_resolution = build_financial_resolution(
            payment,
            allow_refund=(
                policy_supports_refund
                and policy_evidence_loaded
                and not unresolved_conflict
                and not unsupported_claim
            ),
        )
        resolution_actions = self._resolution_actions(
            primary_issue,
            payment,
            financial_resolution.recommended_refund_brl,
            policy_supports_refund=policy_supports_refund,
            policy_forbids_refund=policy_forbids_refund,
            unresolved_conflict=unresolved_conflict,
            policy_evidence_loaded=policy_evidence_loaded,
            policy_required=policy_is_needed(topics),
        )
        case_status = self._case_status(
            primary_issue,
            shipment,
            payment,
            resolution_actions,
            unresolved_conflict=unresolved_conflict,
            policy_evidence_loaded=policy_evidence_loaded,
            policy_required=policy_is_needed(topics),
        )
        confidence = self._confidence(
            entity,
            shipment,
            payment,
            policy_data is not None,
            unresolved_conflict=unresolved_conflict,
        )
        secondary_issues = self._secondary_issues(primary_issue, topics, shipment, payment)

        decision_code = "CONFLICT_UNRESOLVED" if unresolved_conflict else "CONFLICT_RESOLVED"
        if not conflicts:
            decision_code = (
                "POLICY_REFUND_ELIGIBLE" if policy_supports_refund else "POLICY_NO_REFUND"
            )
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy-agent",
            decision_code=decision_code,
            evidence_refs=all_evidence_refs or None,
        )
        policy_data: Any = None

        policy_version = case.get("policy_version")
        if not isinstance(policy_version, str) and isinstance(case.get("policy"), Mapping):
            policy_version = case["policy"].get("version")
        available = await _available_tools(gateway)
        policy_tool = _choose_tool(available, "get_policy")
        if (
            isinstance(case_id, str)
            and case_id.strip()
            and isinstance(policy_version, str)
            and policy_version.strip()
            and policy_tool is not None
        ):
            try:
                policy_evidence = await gateway.call(
                    policy_tool,
                    case_id=case_id.strip(),
                    policy_version=policy_version.strip(),
                )
            except Exception:  # noqa: BLE001 - policy absence requires investigation
                policy_evidence = None
            if policy_evidence is not None:
                policy_data = evidence_data(policy_evidence)
                evidence_refs.extend(
                    _trace_evidence(trace, case_id.strip(), policy_tool, policy_evidence)
                )

        topics = _claim_topics(case)
        payment_issue = _payment_issue(payment)
        shipment_issue = _shipment_issue(shipment)
        claim_issue = next(
            (
                topic
                for topic in topics
                if topic
                in {
                    "canceled_order_paid",
                    "unavailable_order_paid",
                    "late_delivery_seller",
                    "late_delivery_logistics",
                    "valid_split_payment",
                    "payment_mismatch",
                    "duplicate_charge",
                    "refund_pending",
                    "refund_failed",
                }
            ),
            None,
        )

        if payment_issue is not None:
            primary_issue = payment_issue
        elif shipment_issue is not None:
            primary_issue = shipment_issue
        elif (
            claim_issue in {"canceled_order_paid", "unavailable_order_paid"}
            and payment.captured_total_brl
        ) or (claim_issue == "valid_split_payment" and payment.verdict == "reconciled"):
            primary_issue = claim_issue
        elif payment.verdict == "refunded":
            primary_issue = "unsupported_claim"
        else:
            primary_issue = "insufficient_evidence"

        secondary: list[str] = []
        for issue in (payment_issue, shipment_issue, claim_issue):
            if issue and issue != primary_issue and issue not in secondary:
                secondary.append(issue)
        if shipment.verdict in {"lost", "returned"} and shipment.verdict not in secondary:
            secondary.append(shipment.verdict)

        evidence_is_sufficient = bool(evidence_refs) and entity.resolution_status == "resolved"
        if primary_issue == "insufficient_evidence":
            status = "needs_investigation"
            confidence = 0.2 if evidence_is_sufficient else 0.0
        elif primary_issue == "unsupported_claim":
            status = "no_action"
            confidence = 0.82
        else:
            status = "action_required"
            confidence = 0.9 if evidence_is_sufficient else 0.55

        ranked_causes: list[RankedCause] = []
        responsible_parties: list[ResponsibleParty] = []
        if primary_issue != "insufficient_evidence":
            cause, parties = _cause_and_party(primary_issue, shipment)
            ranked_causes.append(cause)
            responsible_parties.extend(parties)

        conflicts: list[DataConflict] = []
        if shipment.verdict == "conflicting":
            conflicts.append(
                DataConflict(
                    field="shipment_status",
                    sources=["get_order", "get_shipment_summary"],
                    selected_source=None,
                    resolution_code="manual_review_required",
                )
            )
        if payment.verdict == "capture_mismatch":
            conflicts.append(
                DataConflict(
                    field="payment_total",
                    sources=["get_order", "get_payment_timeline"],
                    selected_source="get_order",
                    resolution_code="captured_amount_differs_from_order_total",
                )
            )

        refund_allowed = _policy_allows_refund(policy_data)
        refund_amount = payment.refundable_total_brl
        if refund_amount is not None and refund_allowed is False:
            refund_amount = 0.0
        policy_limit = _policy_money(policy_data)
        if refund_amount is not None and policy_limit is not None:
            refund_amount = min(refund_amount, policy_limit)
        if (
            primary_issue not in {"refund_pending", "refund_failed"}
            and "requested_full_refund" not in topics
        ):
            refund_amount = 0.0
        refund_amount = round(max(refund_amount or 0.0, 0.0), 2)

        refund_lines: list[RefundLine] = []
        if refund_amount > 0:
            refund_lines.append(
                RefundLine(
                    reason_code=primary_issue,
                    amount_brl=refund_amount,
                    entity_id=entity.resolved_order_id,
                )
            )
        financial = FinancialResolution(
            recommended_refund_brl=refund_amount,
            refund_lines=refund_lines,
        )

        actions: list[str] = []
        if primary_issue in {"refund_pending", "refund_failed"}:
            actions.append("review_refund_lifecycle")
        elif primary_issue in {"payment_mismatch", "duplicate_charge"}:
            actions.append("reconcile_payment_with_order_total")
        elif primary_issue == "late_delivery_seller":
            actions.append("contact_seller_for_delivery_review")
        elif primary_issue == "late_delivery_logistics":
            actions.append("escalate_shipment_to_logistics")
        elif primary_issue in {"canceled_order_paid", "unavailable_order_paid"}:
            actions.append("review_order_fulfillment_and_refund")
        elif primary_issue == "unsupported_claim":
            actions.append("close_case_with_evidence")
        else:
            actions.append("collect_missing_case_evidence")

        if isinstance(case_id, str) and case_id.strip():
            trace.emit(
                case_id=case_id.strip(),
                event_type="policy_decided",
                actor=_ACTOR,
                decision_code=primary_issue,
            )

        return PolicyResult(
            primary_issue=primary_issue,
            secondary_issues=secondary_issues,
            case_status=case_status,
            confidence=confidence,
            ranked_causes=ranked_causes,
            responsible_parties=responsible_parties,
            data_conflicts=conflicts,
            financial_resolution=financial_resolution,
            resolution_actions=resolution_actions,
            evidence_refs=all_evidence_refs,
        )

    @staticmethod
    async def _discover_policy_tool(gateway: EvidenceGateway) -> str | None:
        try:
            tools = await gateway.list_tools()
        except (RuntimeError, ValueError):
            return None
        if "get_policy" in tools:
            return "get_policy"
        if "search_policy" in tools:
            return "search_policy"
        return None

    @staticmethod
    def _is_unsupported_claim(
        topics: list[str],
        shipment: ShipmentResult,
        payment: PaymentResult,
        *,
        policy_forbids_refund: bool,
    ) -> bool:
        late_claim = bool(set(topics) & {"late_delivery_seller", "late_delivery_logistics"})
        payment_claim = bool(set(topics) & {"payment_mismatch", "duplicate_charge"})
        shipment_disagrees = late_claim and shipment.verdict == "on_time"
        payment_disagrees = payment_claim and payment.verdict in {"reconciled", "refunded"}
        refund_disallowed = policy_forbids_refund and "requested_full_refund" in topics
        return refund_disallowed or shipment_disagrees or payment_disagrees

    @staticmethod
    def _resolution_actions(
        primary_issue: str,
        payment: PaymentResult,
        refund_amount: float,
        *,
        policy_supports_refund: bool,
        policy_forbids_refund: bool,
        unresolved_conflict: bool,
        policy_evidence_loaded: bool,
        policy_required: bool,
    ) -> list[str]:
        if (
            unresolved_conflict
            or policy_forbids_refund
            or (policy_required and not policy_evidence_loaded)
        ):
            return []
        if payment.verdict == "refunded":
            return []
        if refund_amount > 0 and policy_supports_refund:
            return ["ISSUE_REFUND"]
        actions = {
            "late_delivery_seller": "ESCALATE_SELLER_DELAY",
            "late_delivery_logistics": "ESCALATE_LOGISTICS_DELAY",
            "payment_mismatch": "INVESTIGATE_PAYMENT_MISMATCH",
            "duplicate_charge": "INVESTIGATE_DUPLICATE_CAPTURE",
            "refund_pending": "MONITOR_REFUND",
            "refund_failed": "REVIEW_REFUND_FAILURE",
        }
        action = actions.get(primary_issue)
        return [action] if action else []

    @staticmethod
    def _refund_issue(payment: PaymentResult, current_issue: str) -> str:
        if payment.verdict == "refunded":
            return "unsupported_claim"
        if payment.verdict in {"refund_pending", "refund_failed"}:
            return payment.verdict
        return current_issue if current_issue != "insufficient_evidence" else "unsupported_claim"

    @staticmethod
    def _case_status(
        primary_issue: str,
        shipment: ShipmentResult,
        payment: PaymentResult,
        actions: list[str],
        *,
        unresolved_conflict: bool,
        policy_evidence_loaded: bool,
        policy_required: bool,
    ) -> str:
        if (
            unresolved_conflict
            or primary_issue == "insufficient_evidence"
            or (policy_required and not policy_evidence_loaded)
        ):
            return "needs_investigation"
        if actions:
            return "action_required"
        if primary_issue == "unsupported_claim":
            return "no_action"
        if (
            shipment.verdict == "insufficient_evidence"
            and payment.verdict == "insufficient_evidence"
        ):
            return "needs_investigation"
        return "no_action"

    @staticmethod
    def _confidence(
        entity: EntityResult,
        shipment: ShipmentResult,
        payment: PaymentResult,
        policy_loaded: bool,
        *,
        unresolved_conflict: bool,
    ) -> float:
        scores: list[float] = []
        if entity.resolution_status == "resolved":
            scores.append(entity.confidence)
        elif entity.resolution_status == "ambiguous":
            scores.append(min(entity.confidence, 0.5))
        else:
            scores.append(0.0)
        shipment_supported = (
            shipment.verdict != "insufficient_evidence" and shipment.timeline_complete
        )
        scores.append(0.85 if shipment_supported else 0.0)
        scores.append(
            0.85 if payment.verdict != "insufficient_evidence" and payment.evidence_refs else 0.0
        )
        if policy_loaded:
            scores.append(0.9)
        confidence = min(scores) if scores else 0.0
        if unresolved_conflict:
            confidence = min(confidence, 0.45)
        return round(max(0.0, min(confidence, 1.0)), 4)

    @staticmethod
    def _secondary_issues(
        primary_issue: str,
        topics: list[str],
        shipment: ShipmentResult,
        payment: PaymentResult,
    ) -> list[str]:
        candidates = list(topics)
        if shipment.verdict in {"seller_delay", "logistics_delay"}:
            candidates.append(
                "late_delivery_seller"
                if shipment.verdict == "seller_delay"
                else "late_delivery_logistics"
            )
        if payment.verdict in {
            "capture_mismatch",
            "duplicate_capture",
            "refund_pending",
            "refund_failed",
        }:
            candidates.append(
                {
                    "capture_mismatch": "payment_mismatch",
                    "duplicate_capture": "duplicate_charge",
                    "refund_pending": "refund_pending",
                    "refund_failed": "refund_failed",
                }[payment.verdict]
            )
        return [
            issue
            for issue in dedupe(candidates)
            if issue in PRIMARY_ISSUES and issue != primary_issue
        ][:10]
