from __future__ import annotations
from typing import Any
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

class PaymentAgent:
    """Người 4: Phụ trách Giao dịch và Hoàn tiền."""
    
    async def check_transactions(self, entity_data: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> dict[str, Any]:
        """
        Nhiệm vụ: Kiểm tra payment, refund status.
        Gọi MCP tools: check_payment, check_refund...
        """
        # TODO: Implement LLM logic & tool calls here
        return {
            "payment_status": "TODO",
            "refund_status": "TODO",
            "evidence_refs": []
        }
