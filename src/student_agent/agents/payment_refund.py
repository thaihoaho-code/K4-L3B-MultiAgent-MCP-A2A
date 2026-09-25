from __future__ import annotations

from ..agent_types import AgentResult, AgentTask
from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


async def run_payment_refund_agent(
    task: AgentTask, gateway: EvidenceGateway, trace: TraceWriter
) -> AgentResult:
    """Hoàng: reconcile captures and refunds for resolved orders."""
    raise NotImplementedError("Phase 2: implement payment/refund investigation")
