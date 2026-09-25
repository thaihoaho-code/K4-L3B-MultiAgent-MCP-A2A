from __future__ import annotations
from typing import Any
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .entity_agent import EntityAgent
from .order_shipment_agent import OrderShipmentAgent
from .payment_agent import PaymentAgent
from .policy_agent import PolicyAgent
from .verifier_agent import VerifierAgent

class CoordinatorAgent:
    """Người 1: Phụ trách Điều phối luồng và Giao tiếp A2A."""
    
    def __init__(self) -> None:
        self.entity_agent = EntityAgent()
        self.order_shipment_agent = OrderShipmentAgent()
        self.payment_agent = PaymentAgent()
        self.policy_agent = PolicyAgent()
        self.verifier_agent = VerifierAgent()

    async def execute_workflow(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> dict[str, Any]:
        """
        Nhiệm vụ: Gọi các agent tuần tự hoặc song song.
        Ghi log (trace.emit) khi handoff giữa các tác tử.
        """
        trace.emit(case_id=case["case_id"], event_type="task_assigned", actor="coordinator")
        
        # 1. Gọi Entity Agent
        entity_data = await self.entity_agent.resolve(case, gateway, trace)
        
        trace.emit(case_id=case["case_id"], event_type="handoff", actor="coordinator", target="order_shipment_agent")
        trace.emit(case_id=case["case_id"], event_type="handoff", actor="coordinator", target="payment_agent")
        
        # 2. Các agent chuyên môn thu thập dữ liệu
        # Thực tế nên dùng asyncio.gather để chạy song song giúp tăng efficiency
        order_data = await self.order_shipment_agent.investigate(entity_data, gateway, trace)
        payment_data = await self.payment_agent.check_transactions(entity_data, gateway, trace)
        
        trace.emit(case_id=case["case_id"], event_type="handoff", actor="coordinator", target="policy_agent")
        
        # 3. Gom dữ liệu cho Policy Agent xử lý
        investigation_data = {
            "order_data": order_data,
            "payment_data": payment_data
        }
        conclusion_data = await self.policy_agent.resolve_conflict(investigation_data, gateway, trace)
        
        trace.emit(case_id=case["case_id"], event_type="handoff", actor="coordinator", target="verifier_agent")
        
        # 4. Verifier kiểm tra và xuất JSON
        final_output = self.verifier_agent.verify_and_format(case, entity_data, conclusion_data)
        
        return final_output
