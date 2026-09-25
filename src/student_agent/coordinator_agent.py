from __future__ import annotations

import asyncio
from typing import Any

from .entity_agent import EntityAgent
from .mcp_gateway import EvidenceGateway
from .order_shipment_agent import OrderShipmentAgent
from .payment_agent import PaymentAgent
from .policy_agent import PolicyAgent
from .trace import TraceWriter
from .verifier_agent import VerifierAgent


class CoordinatorAgent:
    """Người 1: Phụ trách Điều phối luồng và Giao tiếp A2A."""

    def __init__(self) -> None:
        self.entity_agent = EntityAgent()
        self.order_shipment_agent = OrderShipmentAgent()
        self.payment_agent = PaymentAgent()
        self.policy_agent = PolicyAgent()
        self.verifier_agent = VerifierAgent()

    async def execute_workflow(
        self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
    ) -> dict[str, Any]:
        """
        Điều phối luồng Multi-Agent:
          1. EntityAgent  → resolve customer + order
          2. OrderShipmentAgent ║ PaymentAgent  → song song (asyncio.gather)
          3. PolicyAgent  → phân xử conflict + policy
          4. VerifierAgent → đóng gói JSON output chuẩn
        """
        case_id = case["case_id"]

        # --- Bước 1: Entity Resolution ---
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="entity_agent",
        )
        entity = await self.entity_agent.resolve(case, gateway, trace)

        # --- Bước 2: Specialist Agents chạy song song ---
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="coordinator",
            target="order_shipment_agent",
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="coordinator",
            target="payment_agent",
        )

        shipment, payment = await asyncio.gather(
            self.order_shipment_agent.investigate(entity, gateway, trace, case_id),
            self.payment_agent.check_transactions(
                entity,
                gateway,
                trace,
                case_id=case_id,
                case=case,
            ),
        )

        # --- Bước 3: Policy + Conflict Resolution ---
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="coordinator",
            target="policy_agent",
        )
        policy = await self.policy_agent.resolve_conflict(
            case, entity, shipment, payment, gateway, trace
        )

        # --- Bước 4: Verify + Format Output ---
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="coordinator",
            target="verifier_agent",
        )
        output = self.verifier_agent.verify_and_format(
            case, entity, shipment, payment, policy, trace
        )

        return output
