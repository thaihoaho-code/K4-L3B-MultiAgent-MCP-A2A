from __future__ import annotations

from ..agent_types import AgentResult, AgentTask
from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


async def run_policy_agent(
    task: AgentTask, gateway: EvidenceGateway, trace: TraceWriter
) -> AgentResult:
    """Hồng: apply relevant MCP policy evidence to specialist findings."""
    raise NotImplementedError("Phase 2: implement policy decisions")
