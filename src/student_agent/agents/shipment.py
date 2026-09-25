from __future__ import annotations

from ..agent_types import AgentResult, AgentTask
from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


async def run_shipment_agent(
    task: AgentTask, gateway: EvidenceGateway, trace: TraceWriter
) -> AgentResult:
    """Phúc: establish shipment timeline and delay attribution."""
    raise NotImplementedError("Phase 2: implement shipment investigation")
