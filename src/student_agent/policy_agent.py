from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .state import (
    DataConflict,
    EntityResult,
    FinancialResolution,
    PaymentResult,
    PolicyResult,
    RankedCause,
    ResponsibleParty,
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
        # TODO: Implement LLM logic & tool calls here
        # Ví dụ:
        # evidence = await gateway.call(
        #     "get_policy",
        #     case_id=case["case_id"],
        #     policy_version=case["policy_version"],
        # )
        # trace.emit(
        #     case_id=case["case_id"],
        #     event_type="tool_result_consumed",
        #     actor="policy_agent",
        #     tool_name="get_policy",
        #     evidence_refs=[evidence["evidence_ref"]],
        # )
        # trace.emit(
        #     case_id=case["case_id"],
        #     event_type="policy_decided",
        #     actor="policy_agent",
        #     decision_code="LOGISTICS_DELAY_REFUND",
        # )

        # Gom tất cả evidence_refs từ các agent upstream
        all_evidence_refs = (
            entity.evidence_refs + shipment.evidence_refs + payment.evidence_refs
        )

        return PolicyResult(
            primary_issue="insufficient_evidence",
            secondary_issues=[],
            case_status="needs_investigation",
            confidence=0.0,
            ranked_causes=[],
            responsible_parties=[],
            data_conflicts=[],
            financial_resolution=FinancialResolution(),
            resolution_actions=[],
            evidence_refs=all_evidence_refs,
        )
