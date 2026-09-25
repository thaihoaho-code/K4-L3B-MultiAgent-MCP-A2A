from __future__ import annotations

from typing import Any

from .state import (
    ClaimAssessment,
    EntityResult,
    PaymentResult,
    PolicyResult,
    ShipmentResult,
)
from .trace import TraceWriter


class VerifierAgent:
    """Người 6: Phụ trách Validate, Format JSON và Tối ưu Efficiency."""

    def verify_and_format(
        self,
        case: dict[str, Any],
        entity: EntityResult,
        shipment: ShipmentResult,
        payment: PaymentResult,
        policy: PolicyResult,
        trace: TraceWriter,
        claim_assessments: list[ClaimAssessment] | None = None,
    ) -> dict[str, Any]:
        """
        Nhiệm vụ: Gom kết quả từ tất cả agent, kiểm tra và đóng gói thành JSON output chuẩn.

        Kiểm tra trước khi return:
          - evidence_refs không rỗng
          - resolved_order_id khớp với order_ids trong shipment
          - confidence nằm trong [0, 1]
          - resolution_actions không vượt quá 8 items
        """
        case_id = case["case_id"]

        # Gom toàn bộ evidence refs từ các agent
        all_evidence_refs = sorted(set(
            entity.evidence_refs
            + shipment.evidence_refs
            + payment.evidence_refs
            + policy.evidence_refs
        ))

        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier_agent",
            evidence_refs=all_evidence_refs or None,
        )

        # Build claim_assessments nếu có
        raw_claim_assessments = []
        if claim_assessments:
            for ca in claim_assessments:
                raw_claim_assessments.append({
                    "claim_id": ca.claim_id,
                    "verdict": ca.verdict,
                    "confidence": ca.confidence,
                    "evidence_refs": ca.evidence_refs,
                })

        output: dict[str, Any] = {
            "schema_version": "day09-l3b-output-v2",
            "case_id": case_id,

            "assessment": {
                "primary_issue": policy.primary_issue,
                "secondary_issues": policy.secondary_issues,
                "case_status": policy.case_status,
                "confidence": round(policy.confidence, 4),
            },

            "affected_entities": {
                "order_ids": shipment.order_ids,
                "item_ids": shipment.item_ids,
                "seller_ids": shipment.seller_ids,
                "payment_references": payment.payment_references,
                "shipment_ids": shipment.shipment_ids,
            },

            "entity_resolution": {
                "status": entity.resolution_status,
                "resolved_order_ids": (
                    [entity.resolved_order_id] if entity.resolved_order_id else []
                ),
                "rejected_candidates": entity.rejected_order_ids,
                "confidence": round(entity.confidence, 4),
            },

            "customer_context": {
                "customer_unique_id": entity.customer_unique_id,
                "related_order_ids": entity.related_order_ids,
            },

            "shipment_analysis": {
                "verdict": shipment.verdict,
                "late_seller_ids": shipment.late_seller_ids,
                "timeline_complete": shipment.timeline_complete,
            },

            "payment_analysis": {
                "verdict": payment.verdict,
                "captured_total_brl": payment.captured_total_brl,
                "refunded_total_brl": payment.refunded_total_brl,
                "refundable_total_brl": payment.refundable_total_brl,
            },

            "root_cause_analysis": {
                "ranked_causes": [
                    {"cause_code": rc.cause_code, "rank": rc.rank}
                    for rc in policy.ranked_causes
                ],
                "responsible_parties": [
                    {"party_type": rp.party_type, "party_id": rp.party_id}
                    for rp in policy.responsible_parties
                ],
            },

            "evidence_refs": all_evidence_refs,

            "data_conflicts": [
                {
                    "field": dc.field,
                    "sources": dc.sources,
                    "selected_source": dc.selected_source,
                    "resolution_code": dc.resolution_code,
                }
                for dc in policy.data_conflicts
            ],

            "financial_resolution": {
                "currency": policy.financial_resolution.currency,
                "recommended_refund_brl": policy.financial_resolution.recommended_refund_brl,
                "refund_lines": [
                    {
                        "reason_code": rl.reason_code,
                        "amount_brl": rl.amount_brl,
                        "entity_id": rl.entity_id,
                    }
                    for rl in policy.financial_resolution.refund_lines
                ],
            },

            "resolution_actions": policy.resolution_actions[:8],
        }

        # Thêm claim_assessments nếu có (optional nhưng tăng điểm)
        if raw_claim_assessments:
            output["claim_assessments"] = raw_claim_assessments

        return output
