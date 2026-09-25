from __future__ import annotations

from .mcp_gateway import EvidenceGateway
from .state import EntityResult, ShipmentResult
from .trace import TraceWriter


class OrderShipmentAgent:
    """Người 3: Phụ trách thông tin Đơn hàng và Vận chuyển."""

    async def investigate(
        self,
        entity: EntityResult,
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> ShipmentResult:
        """
        Nhiệm vụ: Lấy thông tin order, product và tracking shipment.

        Input từ entity:
          - entity.resolved_order_id: order ID đã được resolve
          - entity.customer_unique_id: customer ID

        Gọi MCP tools (ví dụ): get_order_details, get_shipment_status...
        Ghi trace: tool_result_consumed
        """
        # TODO: Implement LLM logic & tool calls here
        # Ví dụ:
        # evidence = await gateway.call(
        #     "get_shipment_status",
        #     case_id=...,   # lấy từ case gốc hoặc truyền xuống
        #     order_id=entity.resolved_order_id,
        # )
        # trace.emit(
        #     case_id=...,
        #     event_type="tool_result_consumed",
        #     actor="order_shipment_agent",
        #     tool_name="get_shipment_status",
        #     evidence_refs=[evidence["evidence_ref"]],
        # )
        return ShipmentResult(
            verdict="insufficient_evidence",
            late_seller_ids=[],
            order_ids=[entity.resolved_order_id] if entity.resolved_order_id else [],
            item_ids=[],
            seller_ids=[],
            shipment_ids=[],
            timeline_complete=False,
            evidence_refs=[],
        )
