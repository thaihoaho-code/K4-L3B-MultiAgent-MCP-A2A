from __future__ import annotations
from typing import Any
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

class EntityAgent:
    """Người 2: Phụ trách Entity Resolution và Customer Context."""
    
    async def resolve(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> dict[str, Any]:
        """
        Nhiệm vụ: Dùng thông tin case để tìm customer_unique_id và order_id.
        Gọi MCP tools: get_customer_history, tìm kiếm user...
        """
        # TODO: Implement LLM logic & tool calls here
        return {
            "customer_unique_id": "TODO",
            "order_id": "TODO",
            "customer_context": "TODO"
        }
