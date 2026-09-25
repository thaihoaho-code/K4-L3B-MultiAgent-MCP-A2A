from __future__ import annotations
from typing import Any
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

class PolicyAgent:
    """Người 5: Phụ trách tra cứu Policy và Giải quyết Xung đột."""
    
    async def resolve_conflict(self, investigation_data: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> dict[str, Any]:
        """
        Nhiệm vụ: Đọc policy và phân xử đúng sai dựa trên evidence từ các agent khác.
        Gọi MCP tools: search_policy...
        """
        # TODO: Implement LLM logic & tool calls here
        return {
            "conclusion": "TODO",
            "confidence": "high",
            "evidence_refs": []
        }
