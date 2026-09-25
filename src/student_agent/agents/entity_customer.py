from __future__ import annotations

from ..agent_types import AgentResult, AgentTask
from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


async def run_entity_customer_agent(
    task: AgentTask, gateway: EvidenceGateway, trace: TraceWriter
) -> AgentResult:
    """Phát: resolve candidates and return customer context in data."""
    raise NotImplementedError("Phase 2: implement entity/customer investigation")
