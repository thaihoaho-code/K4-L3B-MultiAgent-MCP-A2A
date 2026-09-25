from __future__ import annotations

from ..agent_types import AgentResult, AgentTask
from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


async def verify_output(
    task: AgentTask, gateway: EvidenceGateway, trace: TraceWriter
) -> AgentResult:
    """Hòa: verify draft output and return data={valid, errors, warnings}."""
    raise NotImplementedError("Phase 2: implement output verification")
