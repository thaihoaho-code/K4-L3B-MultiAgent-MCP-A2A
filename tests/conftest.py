"""Synthetic MCP fixtures shaped like the public evidence envelope (no competition data)."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.evidence_cache import CaseEvidenceStore
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "0123456789abcdef0123456789abcdef"
SELLER = "seller-test0001"
ITEM = "item-test0001"
DOMAINS = {
    "get_order": "order",
    "get_customer_history": "customer",
    "get_order_items": "item",
    "get_product_context": "product",
    "get_shipment_summary": "shipment",
    "get_payment_timeline": "payment",
    "get_order_payments": "payment",
    "get_refund_timeline": "refund",
    "get_sellers": "seller",
    "get_policy": "policy",
}
POLICY_RULES = {
    "canceled_order_paid": ("action_required", "issue_refund", 79.0, "platform"),
    "duplicate_charge": ("action_required", "refund_duplicate_charge", 64.0, "payment_provider"),
    "late_delivery_logistics": ("action_required", "refund_freight", 16.0, "logistics_provider"),
    "late_delivery_seller": ("action_required", "refund_freight", 18.0, "seller"),
    "payment_mismatch": ("action_required", "reconcile_payment", 35.0, "payment_provider"),
    "refund_failed": ("action_required", "retry_refund", 52.0, "payment_provider"),
    "refund_pending": ("needs_investigation", "monitor_refund", 0.0, "payment_provider"),
    "unavailable_order_paid": ("action_required", "issue_refund", 89.0, "seller"),
    "unsupported_claim": ("no_action", "document_no_action", 0.0, "customer"),
    "valid_split_payment": ("no_action", "document_no_action", 0.0, "customer"),
}


def ts(day: str, hour: int = 9) -> str:
    return f"{day}T{hour:02d}:00:00-03:00"


def order_row(purchase: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "order_id": ORDER_ID,
        "customer_id": "customer-row-test",
        "order_status": "delivered",
        "order_purchase_timestamp": ts(purchase),
        "order_approved_at": ts(purchase, 10),
        "order_delivered_carrier_date": ts(_shift(purchase, 2)),
        "order_delivered_customer_date": ts(_shift(purchase, 9)),
        "order_estimated_delivery_date": ts(_shift(purchase, 10)),
    }
    row.update(overrides)
    return row


def _shift(day: str, days: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def policy_data() -> dict[str, Any]:
    return {
        "currency": "BRL",
        "policy_version": "TEST_POLICY",
        "rules": {
            issue: {
                "case_status": status,
                "recommended_action": action,
                "refund_brl": refund,
                "responsible_parties": [
                    {
                        "party_type": party,
                        "party_id": "seller-from-policy" if party == "seller" else None,
                    }
                ],
            }
            for issue, (status, action, refund, party) in POLICY_RULES.items()
        },
    }


def scenario(
    purchase: str = "2018-03-01",
    *,
    status: str = "delivered",
    carrier_days: int = 2,
    delivered_days: int | None = 9,
    limit_days: int = 3,
    freight: str = "10.00",
    price: str = "79.00",
    captures: list[tuple[str, str]] | None = None,
    payment_events: list[dict[str, Any]] | None = None,
    refunds: list[dict[str, Any]] | None = None,
    shipment_events: list[dict[str, Any]] | None = None,
    distractor: bool = True,
) -> dict[str, Any]:
    """Build MCP ``data`` payloads: one in-scope record plus an optional stale distractor."""
    delivered = ts(_shift(purchase, delivered_days)) if delivered_days is not None else None
    scoped = order_row(
        purchase,
        order_status=status,
        order_delivered_carrier_date=ts(_shift(purchase, carrier_days)),
        order_delivered_customer_date=delivered,
    )
    stale = order_row("2019-01-01")
    captures = captures if captures is not None else [("credit_card", "89.00")]
    payments = [
        {
            "order_id": ORDER_ID,
            "payment_sequential": str(index + 1),
            "payment_type": kind,
            "payment_installments": "1",
            "payment_value": amount,
        }
        for index, (kind, amount) in enumerate(captures)
    ]
    events = [
        {
            "order_id": ORDER_ID,
            "event_at": ts(purchase, 10 + index),
            "event_type": "captured",
            "amount_brl": amount,
            "status": "confirmed",
        }
        for index, (_, amount) in enumerate(captures)
    ] + list(payment_events or [])
    items = [
        {
            "order_id": ORDER_ID,
            "order_item_id": ITEM,
            "product_id": "product-test0001",
            "seller_id": SELLER,
            "shipping_limit_date": ts(_shift(purchase, limit_days)),
            "price": price,
            "freight_value": freight,
        }
    ]
    history = [scoped]
    if distractor:
        history.append(stale)
        payments.append(dict(payments[0], payment_value="52.00") if payments else {})
        events.append(
            {
                "order_id": ORDER_ID,
                "event_at": ts("2019-01-01", 10),
                "event_type": "captured",
                "amount_brl": "52.00",
                "status": "confirmed",
            }
        )
        items.append(dict(items[0], shipping_limit_date=ts("2019-01-04"), freight_value="99.00"))
    return {
        "get_order": stale if distractor else scoped,
        "get_customer_history": {"customer_unique_id": "customer-test", "orders": history},
        "get_order_items": items,
        "get_product_context": [
            {
                "order_item_id": ITEM,
                "product_id": "product-test0001",
                "seller_id": SELLER,
                "product": {"product_id": "product-test0001", "product_category_name": "x"},
                "category_name_english": "housewares",
            }
        ],
        "get_shipment_summary": {
            "order_id": ORDER_ID,
            "order_status": (stale if distractor else scoped)["order_status"],
            "delivered_carrier_at": (stale if distractor else scoped)[
                "order_delivered_carrier_date"
            ],
            "delivered_customer_at": (stale if distractor else scoped)[
                "order_delivered_customer_date"
            ],
            "estimated_delivery_at": (stale if distractor else scoped)[
                "order_estimated_delivery_date"
            ],
            "shipping_limits": [
                {
                    "order_item_id": ITEM,
                    "seller_id": SELLER,
                    "shipping_limit_at": row["shipping_limit_date"],
                }
                for row in items
            ],
            "events": list(shipment_events or []),
        },
        "get_payment_timeline": {"order_id": ORDER_ID, "payments": payments, "events": events},
        "get_refund_timeline": (
            {"order_id": ORDER_ID, "events": refunds} if refunds is not None else None
        ),
        "get_sellers": [{"seller_id": SELLER, "seller_city": "x", "seller_state": "SP"}],
        "get_policy": policy_data(),
    }


def make_case(
    case_id: str = "L3B_CASE_T01",
    topic: str = "late_delivery_seller",
    opened_at: str = "2018-03-13T09:00:00-03:00",
    candidates: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "opened_at": opened_at,
        "customer_request": {
            "language": "vi",
            "message": "test",
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": f"{case_id}-a", "topic": topic},
                {"claim_id": f"{case_id}-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "TEST_POLICY",
        "candidate_order_ids": candidates or [ORDER_ID, "candidate-decoy"],
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
            "require_independent_verification": True,
        },
        "customer_unique_id_hint": "customer-test",
    }


class FakeGateway:
    """Serves scenario payloads through the MCP evidence envelope and records every call."""

    def __init__(
        self, data: dict[str, Any], *, transient_failures: int = 0, known_orders: tuple = ()
    ) -> None:
        self.data = data
        self.known_orders = {ORDER_ID, *known_orders}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.transient_failures = transient_failures

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        if self.transient_failures:
            self.transient_failures -= 1
            raise ConnectionError("temporary transport error")
        if "order_id" in arguments and arguments["order_id"] not in self.known_orders:
            raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")
        payload = self.data.get(tool_name)
        if payload is None:
            raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{secrets.token_urlsafe(24)}",
            "result_hash": "sha256:" + "0" * 64,
            "domain": DOMAINS[tool_name],
            "data": payload,
            "warnings": [],
        }

    def tools(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def contracts() -> Contracts:
    return Contracts(ROOT / "contracts" / "schemas")


@pytest.fixture
def trace(tmp_path: Path, contracts: Contracts) -> TraceWriter:
    return TraceWriter(tmp_path / "trace.jsonl", contracts)


@pytest.fixture
def store_factory(trace: TraceWriter):
    def build(data: dict[str, Any], case_id: str = "L3B_CASE_T01", **kwargs: Any):
        gateway = FakeGateway(data, **kwargs)
        return CaseEvidenceStore(case_id, gateway, trace), gateway

    return build
