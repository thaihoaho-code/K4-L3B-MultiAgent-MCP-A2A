from __future__ import annotations

from ..agent_types import AgentResult, AgentTask
from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


async def resolve_conflicts(
    task: AgentTask, gateway: EvidenceGateway, trace: TraceWriter
) -> AgentResult:
    """Hồng: resolve or preserve data conflicts and root cause candidates."""
    raise NotImplementedError("Phase 2: implement conflict resolution")
