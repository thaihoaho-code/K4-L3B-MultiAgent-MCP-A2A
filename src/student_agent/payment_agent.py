from __future__ import annotations

from .mcp_gateway import EvidenceGateway
from .state import EntityResult, PaymentResult
from .trace import TraceWriter


class PaymentAgent:
    """Người 4: Phụ trách Giao dịch và Hoàn tiền."""

    async def check_transactions(
        self,
        entity: EntityResult,
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> PaymentResult:
        """
        Nhiệm vụ: Kiểm tra trạng thái thanh toán và hoàn tiền.

        Input từ entity:
          - entity.resolved_order_id: order ID đã được resolve
          - entity.customer_unique_id: customer ID

        Gọi MCP tools (ví dụ): get_payment_details, get_refund_status...
        Ghi trace: tool_result_consumed
        """
        # TODO: Implement LLM logic & tool calls here
        # Ví dụ:
        # evidence = await gateway.call(
        #     "get_payment_details",
        #     case_id=...,
        #     order_id=entity.resolved_order_id,
        # )
        # trace.emit(
        #     case_id=...,
        #     event_type="tool_result_consumed",
        #     actor="payment_agent",
        #     tool_name="get_payment_details",
        #     evidence_refs=[evidence["evidence_ref"]],
        # )
        return PaymentResult(
            verdict="insufficient_evidence",
            captured_total_brl=None,
            refunded_total_brl=None,
            refundable_total_brl=None,
            payment_references=[],
            evidence_refs=[],
        )
