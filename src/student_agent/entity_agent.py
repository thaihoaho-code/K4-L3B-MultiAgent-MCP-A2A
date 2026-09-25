from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .state import EntityResult
from .trace import TraceWriter


class EntityAgent:
    """Người 2: Phụ trách Entity Resolution và Customer Context."""

    async def resolve(
        self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
    ) -> EntityResult:
        """
        Nhiệm vụ: Dùng thông tin case để tìm customer_unique_id và order_id.

        Input từ case:
          - case["customer_request"]["claimed_order_id"]: order ID khách cung cấp (chưa chắc đúng)
          - case["candidate_order_ids"]: danh sách candidate cần resolve
          - case["customer_unique_id_hint"]: gợi ý customer ID để tra MCP

        Gọi MCP tools (ví dụ): get_customer_history, search_order...
        Ghi trace: task_assigned, tool_result_consumed
        """
        # TODO: Implement LLM logic & tool calls here
        # Ví dụ:
        # evidence = await gateway.call(
        #     "get_customer_history",
        #     case_id=case["case_id"],
        #     customer_unique_id=case["customer_unique_id_hint"],
        # )
        # trace.emit(
        #     case_id=case["case_id"],
        #     event_type="tool_result_consumed",
        #     actor="entity_agent",
        #     tool_name="get_customer_history",
        #     evidence_refs=[evidence["evidence_ref"]],
        # )
        return EntityResult(
            customer_unique_id=None,
            resolved_order_id=None,
            rejected_order_ids=[],
            related_order_ids=[],
            resolution_status="not_found",
            confidence=0.0,
            evidence_refs=[],
        )
