from __future__ import annotations

import asyncio
from typing import Any

from .entity_agent import EntityAgent
from .mcp_gateway import EvidenceGateway
from .order_shipment_agent import OrderShipmentAgent
from .payment_agent import PaymentAgent
from .policy_agent import PolicyAgent
from .state import PaymentResult, ShipmentResult
from .trace import TraceWriter
from .verifier_agent import VerifierAgent


class CoordinatorAgent:
    """Coordinate entity, specialist, policy, and verification stages."""

    def __init__(self) -> None:
        self.entity_agent = EntityAgent()
        self.order_shipment_agent = OrderShipmentAgent()
        self.payment_agent = PaymentAgent()
        self.policy_agent = PolicyAgent()
        self.verifier_agent = VerifierAgent()

    async def execute_workflow(
        self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
    ) -> dict[str, Any]:
        """Run the workflow while keeping unresolved cases evidence-efficient."""

        case_id = case["case_id"]
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="entity_agent",
        )
        entity = await self.entity_agent.resolve(case, gateway, trace)

        # --- Bước 2: Specialist Agents chạy song song ---
        trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="order_shipment_agent")
        trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="payment_agent")

        shipment, payment = await asyncio.gather(
            self.order_shipment_agent.investigate(entity, gateway, trace, case_id),
            self.payment_agent.check_transactions(
                entity,
                gateway,
                trace,
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
                self.order_shipment_agent.investigate(
                    entity,
                    gateway,
                    trace,
                    case_id=case_id,
                    case=case,
                ),
                self.payment_agent.check_transactions(
                    entity,
                    gateway,
                    trace,
                    case_id=case_id,
                    case=case,
                ),
            )
        else:
            shipment = ShipmentResult()
            payment = PaymentResult()

        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="coordinator",
            target="policy_agent",
        )
        policy = await self.policy_agent.resolve_conflict(
            case,
            entity,
            shipment,
            payment,
            gateway,
            trace,
        )

        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="coordinator",
            target="verifier_agent",
        )
        return self.verifier_agent.verify_and_format(
            case,
            entity,
            shipment,
            payment,
            policy,
            trace,
        )
