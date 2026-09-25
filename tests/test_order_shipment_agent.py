from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.order_shipment_agent import OrderShipmentAgent
from student_agent.state import EntityResult
from student_agent.trace import TraceWriter


class Gateway:
    def __init__(self, *, status="delivered", handoff="2025-01-02", delivered="2025-01-04"):
        self.calls = []
        self.payloads = {
            "get_order_details": ("order", {
                "order_id": "ORDER_1", "customer_unique_id": "CUSTOMER_1",
                "items": [
                    {"item_id": item, "seller_id": "SELLER_1", "product_id": "PRODUCT_1",
                     "shipping_limit_date": "2025-01-03"}
                    for item in ("ITEM_1", "ITEM_2")
                ],
            }),
            "get_product_details": ("product", {
                "product_id": "PRODUCT_1", "color": "blue", "size": "M",
                "description": "Cotton shirt",
            }),
            "get_shipment_status": ("shipment", {
                "order_id": "ORDER_1", "shipments": [{
                    "shipment_id": "SHIPMENT_1", "seller_id": "SELLER_1", "status": status,
                    "handed_over_at": handoff, "delivered_at": delivered,
                    "estimated_delivery_at": "2025-01-05", "current_location": "Depot",
                    "observed_at": "2025-01-07", "events": [],
                }],
            }),
        }

    async def list_tools(self):
        return list(self.payloads)

    async def call(self, tool, **kwargs):
        self.calls.append((tool, kwargs))
        domain, data = self.payloads[tool]
        return {
            "schema_version": "day09-mcp-evidence-v1", "domain": domain,
            "evidence_ref": "ev_" + str(len(self.calls)).zfill(20),
            "result_hash": "sha256:" + "0" * 64, "data": deepcopy(data),
        }


def investigate(tmp_path, gateway, entity=None):
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    entity = entity or EntityResult("CUSTOMER_1", "ORDER_1", resolution_status="resolved")
    return asyncio.run(OrderShipmentAgent().investigate(entity, gateway, trace, case_id="CASE_001"))


@pytest.mark.parametrize(("handoff", "delivered", "verdict"), [
    ("2025-01-02", "2025-01-04", "on_time"),
    ("2025-01-04", "2025-01-07", "seller_delay"),
    ("2025-01-02", "2025-01-07", "logistics_delay"),
    (None, "2025-01-07", "insufficient_evidence"),
    ("2025-01-08", "2025-01-07", "conflicting"),
])
def test_delivery_analysis(tmp_path, handoff, delivered, verdict):
    gateway = Gateway(handoff=handoff, delivered=delivered)
    result = investigate(tmp_path, gateway)
    assert result.verdict == verdict
    assert result.products[0]["color"] == "blue"
    assert len(gateway.calls) == 3  # Shared product fetched only once.
    assert all(args["case_id"] == "CASE_001" for _, args in gateway.calls)
    assert len(result.evidence_refs) == 3
    assert result.late_seller_ids == (["SELLER_1"] if verdict == "seller_delay" else [])


@pytest.mark.parametrize("status", ["lost", "returned", "damaged", "in_transit"])
def test_tracking_states(tmp_path, status):
    result = investigate(tmp_path, Gateway(status=status, delivered=None))
    assert result.verdict == (status if status in {"lost", "returned"} else "insufficient_evidence")
    assert not result.timeline_complete
    assert result.shipments[0]["current_location"] == "Depot"
    assert result.shipments[0]["is_delayed"] is True
    assert result.damaged_shipment_ids == (["SHIPMENT_1"] if status == "damaged" else [])


def test_unresolved_entity_makes_no_calls(tmp_path):
    gateway = Gateway()
    result = investigate(tmp_path, gateway, EntityResult("CUSTOMER_1", "ORDER_1"))
    assert result.verdict == "insufficient_evidence"
    assert not gateway.calls
    assert not result.order_ids


def test_wrong_order_is_not_consumed(tmp_path):
    gateway = Gateway()
    gateway.payloads["get_shipment_status"][1]["order_id"] = "OTHER_ORDER"
    result = investigate(tmp_path, gateway)
    assert result.verdict == "insufficient_evidence"
    assert not result.shipments
    assert len(result.evidence_refs) == 2


def test_missing_tools_do_not_trigger_guessed_calls(tmp_path):
    gateway = Gateway()
    gateway.payloads.clear()
    result = investigate(tmp_path, gateway)
    assert not gateway.calls
    assert result.verdict == "insufficient_evidence"


def test_damage_evidence_is_preserved(tmp_path):
    result = investigate(tmp_path, Gateway(status="damaged", delivered=None))
    assert result.evidence[-1]["data"]["shipments"][0]["status"] == "damaged"
    assert "analysis_verdict" not in result.evidence[-1]["data"]["shipments"][0]


def test_mixed_outcomes_are_not_called_source_conflicts(tmp_path):
    gateway = Gateway()
    rows = gateway.payloads["get_shipment_status"][1]["shipments"]
    rows.append({**rows[0], "shipment_id": "SHIPMENT_2", "status": "lost", "delivered_at": None})
    result = investigate(tmp_path, gateway)
    assert result.verdict == "insufficient_evidence"
    assert "mixed_shipment_outcomes" in result.investigation_notes


def test_order_tracking_conflict(tmp_path):
    gateway = Gateway(status="lost", delivered=None)
    gateway.payloads["get_order_details"][1]["order_status"] = "delivered"
    assert investigate(tmp_path, gateway).verdict == "conflicting"


def test_mixed_timezones_do_not_crash_or_invent_delay(tmp_path):
    gateway = Gateway(delivered="2025-01-07T00:00:00Z")
    result = investigate(tmp_path, gateway)
    assert result.verdict == "insufficient_evidence"
    assert not result.timeline_complete


def test_customer_mismatch_stops_related_lookups(tmp_path):
    gateway = Gateway()
    gateway.payloads["get_order_details"][1]["customer_unique_id"] = "OTHER_CUSTOMER"
    result = investigate(tmp_path, gateway)
    assert len(gateway.calls) == 1
    assert not result.order_details
    assert result.verdict == "insufficient_evidence"


def test_tool_timeout_preserves_other_evidence(tmp_path):
    class TimeoutGateway(Gateway):
        async def call(self, tool, **kwargs):
            if tool == "get_product_details":
                raise TimeoutError("unavailable")
            return await super().call(tool, **kwargs)

    result = investigate(tmp_path, TimeoutGateway())
    assert result.verdict == "on_time"
    assert not result.products
    assert len(result.evidence_refs) == 2
    assert "tool_failed:get_product_details:TimeoutError" in result.investigation_notes


def test_split_shipments_need_item_mapping_for_blame(tmp_path):
    gateway = Gateway(delivered="2025-01-07")
    rows = gateway.payloads["get_shipment_status"][1]["shipments"]
    rows.append({**rows[0], "shipment_id": "SHIPMENT_2"})
    result = investigate(tmp_path, gateway)
    assert result.verdict == "insufficient_evidence"
    assert not result.late_seller_ids


def test_unknown_payload_is_reported(tmp_path):
    gateway = Gateway()
    gateway.payloads["get_shipment_status"] = ("shipment", [])
    result = investigate(tmp_path, gateway)
    assert not result.shipments
    assert result.verdict == "insufficient_evidence"
    assert "unsupported_payload:get_shipment_status" in result.investigation_notes
