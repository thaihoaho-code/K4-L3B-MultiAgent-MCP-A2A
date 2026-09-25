from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway
from .state import EntityResult, ShipmentResult
from .trace import TraceWriter


@dataclass(frozen=True)
class SpecialistTools:
    """Bindings cần đối chiếu với MCP server; chỉ gọi tên đã discovery."""

    order: str = "get_order_details"
    product: str = "get_product_details"
    shipment: str = "get_shipment_status"


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _compare(left: Any, right: Any) -> int | None:
    a, b = _date(left), _date(right)
    if a is None or b is None:
        return None
    try:
        return (a > b) - (a < b)
    except TypeError:
        # Không đoán timezone khi dữ liệu trộn naive và aware datetime.
        return None


def _ids(rows: list[dict[str, Any]], key: str) -> list[str]:
    return sorted({row[key] for row in rows if isinstance(row.get(key), str) and row[key]})


class _Investigation:
    """Ngữ cảnh riêng mỗi lần gọi: không cache evidence giữa các case."""

    def __init__(
        self, case_id: str, gateway: EvidenceGateway, trace: TraceWriter,
        result: ShipmentResult, available: set[str],
    ) -> None:
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self.result = result
        self.available = available
        # Chi tiết chỉ tồn tại trong lần điều tra này, không mở rộng state chung.
        # Handoff dùng các field gốc của ShipmentResult và evidence_refs.
        self.order_details: dict[str, Any] = {}
        self.products: list[dict[str, Any]] = []
        self.shipments: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.investigation_notes: list[str] = []
        self.damaged_shipment_ids: list[str] = []

    async def read(
        self, tool: str, domain: str, actor: str, **arguments: str,
    ) -> dict[str, Any] | None:
        if tool not in self.available:
            self.investigation_notes.append(f"tool_unavailable:{tool}")
            return None
        try:
            evidence = await self.gateway.call(tool, case_id=self.case_id, **arguments)
        except (RuntimeError, ValueError, TimeoutError) as exc:
            self.investigation_notes.append(f"tool_failed:{tool}:{type(exc).__name__}")
            return None
        data = evidence.get("data")
        if evidence.get("domain") != domain or not isinstance(data, dict):
            self.investigation_notes.append(f"unsupported_payload:{tool}")
            return None
        # Envelope không có case_id; kiểm tra thêm nếu data có.
        for key, value in {"case_id": self.case_id, **arguments}.items():
            if key in data and data[key] != value:
                self.investigation_notes.append(f"scope_mismatch:{tool}:{key}")
                return None
        ref = evidence["evidence_ref"]
        self.evidence.append(evidence)
        if ref not in self.result.evidence_refs:
            self.result.evidence_refs.append(ref)
        self.investigation_notes.extend(evidence.get("warnings", []))
        self.trace.emit(
            case_id=self.case_id, event_type="tool_result_consumed", actor=actor,
            tool_name=tool, evidence_refs=[ref],
        )
        return data


class OrderProductAgent:
    """Tra cứu order và sản phẩm thuộc các item của order đã resolve."""

    async def investigate(
        self, context: _Investigation, entity: EntityResult, tools: SpecialistTools,
    ) -> list[dict[str, Any]]:
        order = await context.read(
            tools.order, "order", "order_product_agent", order_id=entity.resolved_order_id,
        )
        if order is None:
            return []
        if (
            entity.customer_unique_id is not None
            and "customer_unique_id" in order
            and order["customer_unique_id"] != entity.customer_unique_id
        ):
            context.investigation_notes.append("customer_mismatch")
            return []
        context.order_details = order
        raw_items = order.get("items")
        if not isinstance(raw_items, list) or any(not isinstance(x, dict) for x in raw_items):
            context.investigation_notes.append("missing_or_invalid_items")
            return []
        items = []
        for item in raw_items:
            if item.get("order_id", entity.resolved_order_id) != entity.resolved_order_id:
                context.investigation_notes.append("item_order_mismatch")
                continue
            items.append(item)
        context.result.item_ids = _ids(items, "item_id")
        context.result.seller_ids = _ids(items, "seller_id")
        for product_id in _ids(items, "product_id"):
            product = await context.read(
                tools.product, "product", "order_product_agent", product_id=product_id,
            )
            if product is not None:
                # Giữ nguyên color, size, description; không suy ra từ tên/category.
                context.products.append({**product, "product_id": product_id})
        return items


class ShipmentAgent:
    """Đọc tracking, so sánh mốc thời gian và giữ bằng chứng cho PolicyAgent."""

    async def investigate(
        self, context: _Investigation, order_id: str, tools: SpecialistTools,
        items: list[dict[str, Any]],
    ) -> None:
        data = await context.read(
            tools.shipment, "shipment", "shipment_agent", order_id=order_id,
        )
        if data is None:
            return
        rows = data.get("shipments")
        if not isinstance(rows, list) or not rows or any(not isinstance(x, dict) for x in rows):
            context.investigation_notes.append("missing_or_invalid_shipments")
            return
        if any(row.get("order_id", order_id) != order_id for row in rows):
            context.investigation_notes.append("shipment_order_mismatch")
            return
        result = context.result
        # Evidence gốc nằm ở context.evidence; thêm kết quả phân tích cho mỗi kiện.
        context.shipments = [dict(row) for row in rows]
        result.shipment_ids = _ids(rows, "shipment_id")
        context.damaged_shipment_ids = _ids(
            [row for row in rows if row.get("status") == "damaged"], "shipment_id",
        )
        verdicts = []
        complete = []
        for row in context.shipments:
            handoff = row.get("handed_over_at")
            delivered = row.get("delivered_at")
            expected = row.get("estimated_delivery_at")
            sequence = _compare(delivered, handoff)
            arrival = _compare(delivered, expected)
            complete.append(sequence is not None and sequence >= 0 and arrival is not None)
            status = row.get("status")
            if not isinstance(status, str):
                status = None
            # Dùng thời điểm snapshot của nguồn, không dùng đồng hồ hiện tại cho case lịch sử.
            delay = _compare(
                delivered if status == "delivered" else row.get("observed_at"), expected,
            )
            row["is_delayed"] = delay > 0 if delay is not None else None
            if sequence is not None and sequence < 0:
                verdicts.append("conflicting")
                continue
            if status in {"lost", "returned"}:
                verdicts.append(status)
                continue
            if status != "delivered" or delivered is None:
                # Không suy ra lost hoặc lỗi logistics chỉ vì thiếu scan / đang trễ.
                verdicts.append("insufficient_evidence")
                continue
            seller_id = row.get("seller_id")
            seller_shipments = [x for x in rows if x.get("seller_id") == seller_id]
            seller_items = [
                item for item in items
                if seller_id and item.get("seller_id") == seller_id
                and (
                    item.get("shipment_id") == row.get("shipment_id")
                    and item.get("shipment_id") is not None
                    or "shipment_id" not in item and len(seller_shipments) == 1
                )
            ]
            deadlines = [
                _compare(handoff, item.get("shipping_limit_date")) for item in seller_items
            ]
            if arrival is None:
                verdicts.append("insufficient_evidence")
            elif arrival <= 0:
                verdicts.append("on_time")
            elif seller_id and any(value == 1 for value in deadlines):
                result.late_seller_ids.append(seller_id)
                verdicts.append("seller_delay")
            elif deadlines and all(value is not None and value <= 0 for value in deadlines):
                verdicts.append("logistics_delay")
            else:
                verdicts.append("insufficient_evidence")
        result.timeline_complete = all(complete)
        result.late_seller_ids = sorted(set(result.late_seller_ids))
        distinct = set(verdicts)
        # Các kiện khác kết quả không đồng nghĩa nguồn mâu thuẫn.
        if "conflicting" in distinct:
            result.verdict = "conflicting"
        elif len(distinct) == 1:
            result.verdict = verdicts[0]
        else:
            context.investigation_notes.append("mixed_shipment_outcomes")
        for row, verdict in zip(context.shipments, verdicts, strict=True):
            row["analysis_verdict"] = verdict
        order_status = context.order_details.get("order_status")
        if order_status == "delivered" and any(row.get("status") == "lost" for row in rows):
            result.verdict = "conflicting"
            context.investigation_notes.append("order_shipment_status_conflict")
        if context.damaged_shipment_ids:
            context.investigation_notes.append("damage_requires_policy_assessment")


class OrderShipmentAgent:
    """Facade A2A chỉ trả các field ShipmentResult trong state.py gốc.

    Chi tiết product/tracking được xử lý nội bộ; evidence_refs trỏ tới dữ liệu MCP.
    Không gắn field động vào result hoặc giữ dữ liệu case trên instance agent.
    case_id vẫn bắt buộc vì EntityResult gốc không chứa ID của case.
    """

    def __init__(self, tools: SpecialistTools | None = None) -> None:
        self.tools = tools or SpecialistTools()
        self.order_product_agent = OrderProductAgent()
        self.shipment_agent = ShipmentAgent()

    async def investigate(
        self, entity: EntityResult, gateway: EvidenceGateway, trace: TraceWriter,
        *, case_id: str,
    ) -> ShipmentResult:
        if not case_id:
            raise ValueError("case_id is required for evidence isolation")
        result = ShipmentResult()
        if (
            entity.resolution_status != "resolved" or not entity.resolved_order_id
            or entity.resolved_order_id in entity.rejected_order_ids
        ):
            trace.emit(
                case_id=case_id, event_type="handoff", actor="order_shipment_agent",
                target="coordinator", decision_code="entity_not_resolved",
            )
            return result
        result.order_ids = [entity.resolved_order_id]
        try:
            available = set(await gateway.list_tools())
        except (RuntimeError, ValueError, TimeoutError) as exc:
            trace.emit(
                case_id=case_id, event_type="handoff", actor="order_shipment_agent",
                target="coordinator", decision_code="discovery_failed",
                attributes={"error_type": type(exc).__name__},
            )
            return result
        context = _Investigation(case_id, gateway, trace, result, available)
        items = await self.order_product_agent.investigate(context, entity, self.tools)
        if "customer_mismatch" not in context.investigation_notes:
            await self.shipment_agent.investigate(
                context, entity.resolved_order_id, self.tools, items,
            )
        trace.emit(
            case_id=case_id, event_type="handoff", actor="order_shipment_agent",
            target="coordinator", attributes={
                "verdict": result.verdict,
                "product_count": len(context.products),
                "damaged_shipment_count": len(context.damaged_shipment_ids),
                "investigation_note_count": len(context.investigation_notes),
            },
        )
        return result
