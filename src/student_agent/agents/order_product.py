from __future__ import annotations

from ..agent_types import AgentResult, AgentTask
from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


async def run_order_product_agent(
    task: AgentTask, gateway: EvidenceGateway, trace: TraceWriter
) -> AgentResult:
    """Phúc: investigate resolved orders, items, products and sellers."""
    raise NotImplementedError("Phase 2: implement order/product investigation")
