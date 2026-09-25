from __future__ import annotations

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


class PolicyAgent:
    """Người 5: Phụ trách tra cứu Policy và Giải quyết Xung đột."""

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
