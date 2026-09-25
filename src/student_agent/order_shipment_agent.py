from __future__ import annotations
from typing import Any
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

class OrderShipmentAgent:
    """Người 3: Phụ trách thông tin Đơn hàng và Vận chuyển."""
    
    async def investigate(self, entity_data: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> dict[str, Any]:
        """
        Nhiệm vụ: Lấy thông tin order, product và tracking shipment.
        Gọi MCP tools: get_order_details, get_shipment_status...
        """
        # TODO: Implement LLM logic & tool calls here
        return {
            "order_details": "TODO",
            "shipment_status": "TODO",
            "evidence_refs": []
        }
