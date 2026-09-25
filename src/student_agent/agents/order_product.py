"""Order/product agent: affected items, sellers, products and order-value facts.

Item rows are scoped to the case window through their ``shipping_limit_date``. The agent also
answers the coordinator's ``verify_seller`` follow-up by checking seller records, which is
only requested when policy assigns responsibility to a seller.
"""

from __future__ import annotations

from typing import Any

from ..agent_types import ORDER_AGENT, AgentResult, AgentTask, Status
from ..evidence_cache import CaseEvidenceStore
from ..money import to_decimal, total
from ..scoping import ScopeWindow


def _rows(data: Any) -> list[dict[str, Any]]:
    """Dict rows with exact duplicates removed (the same record served twice is one record)."""
    if not isinstance(data, list):
        return []
    unique: dict[str, dict[str, Any]] = {}
    for row in data:
        if isinstance(row, dict):
            unique.setdefault(repr(sorted(row.items(), key=lambda item: item[0])), row)
    return list(unique.values())


def _unique(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))


def scope_items(rows: list[dict[str, Any]], window: ScopeWindow | None) -> tuple[list, bool]:
    """Return item rows inside the case window, or all rows when none can be scoped."""
    if window is None:
        return rows, False
    scoped = [row for row in rows if window.contains(row.get("shipping_limit_date"))]
    return (scoped, True) if scoped else (rows, False)


async def run_order_product_agent(task: AgentTask, store: CaseEvidenceStore) -> AgentResult:
    order_id = task.payload["order_id"]
    window: ScopeWindow | None = task.payload.get("window")
    scoped_row = task.payload.get("scoped_row") or {}
    warnings: list[str] = []
    refs: list[str] = []

    items_evidence = await store.fetch(ORDER_AGENT, "get_order_items", order_id=order_id)
    item_rows = _rows(items_evidence.data if items_evidence else None)
    scoped_items, scoped_ok = scope_items(item_rows, window)
    if items_evidence and item_rows:
        refs.append(store.consume(ORDER_AGENT, items_evidence))
        if not scoped_ok:
            warnings.append("item_rows_not_window_scoped")
    else:
        warnings.append("order_items_unavailable")

    product_rows: list[dict[str, Any]] = []
    if task.payload.get("include_product_context", True):
        products = await store.fetch(ORDER_AGENT, "get_product_context", order_id=order_id)
        product_rows = _rows(products.data if products else None)
        if products and product_rows:
            refs.append(store.consume(ORDER_AGENT, products))

    item_ids = _unique([row.get("order_item_id") for row in scoped_items])
    seller_ids = _unique([row.get("seller_id") for row in scoped_items])
    product_ids = _unique([row.get("product_id") for row in scoped_items])
    if not item_rows:
        item_ids = _unique([row.get("order_item_id") for row in product_rows])
        seller_ids = _unique([row.get("seller_id") for row in product_rows])
        product_ids = _unique([row.get("product_id") for row in product_rows])
    categories = _unique(
        [
            row.get("category_name_english")
            for row in product_rows
            if row.get("product_id") in product_ids
        ]
    )

    price_total = total(row.get("price") for row in scoped_items) if scoped_items else None
    freight_total = (
        total(row.get("freight_value") for row in scoped_items) if scoped_items else None
    )
    order_value = (
        price_total + freight_total
        if price_total is not None and freight_total is not None
        else None
    )
    order_status = scoped_row.get("order_status")
    hints = []
    if order_status == "canceled":
        hints.append("ORDER_CANCELED")
    if order_status == "unavailable":
        hints.append("ORDER_UNAVAILABLE")

    found = bool(item_ids)
    return AgentResult.reply(
        task,
        status=Status.OK if found else Status.INSUFFICIENT_EVIDENCE,
        confidence=0.95 if found and scoped_ok else (0.7 if found else 0.2),
        decision_code="ORDER_ANALYZED" if found else "ORDER_INSUFFICIENT_EVIDENCE",
        evidence_refs=[ref for ref in refs if ref],
        warnings=warnings,
        data={
            "order_ids": [order_id],
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "product_ids": product_ids,
            "categories": categories,
            "order_status": order_status,
            "price_total": price_total,
            "freight_total": freight_total,
            "order_value": order_value,
            "item_prices": [to_decimal(row.get("price")) for row in scoped_items],
            "root_cause_hints": hints,
        },
    )


async def run_seller_verification(task: AgentTask, store: CaseEvidenceStore) -> AgentResult:
    order_id = task.payload["order_id"]
    requested = _unique(task.payload.get("seller_ids", []))
    sellers = await store.fetch(ORDER_AGENT, "get_sellers", order_id=order_id)
    rows = _rows(sellers.data if sellers else None)
    known = {str(row.get("seller_id")) for row in rows}
    refs = [store.consume(ORDER_AGENT, sellers)] if sellers and rows else []
    verified = [seller for seller in requested if seller in known]
    unknown = [seller for seller in requested if seller not in known]
    ok = bool(verified) and not unknown
    return AgentResult.reply(
        task,
        status=Status.OK if ok else Status.NEEDS_FOLLOWUP,
        confidence=0.95 if ok else 0.4,
        decision_code="SELLER_VERIFIED" if ok else "SELLER_UNVERIFIED",
        evidence_refs=[ref for ref in refs if ref],
        warnings=[] if ok else ["seller_record_missing"],
        data={"verified_seller_ids": verified, "unknown_seller_ids": unknown},
    )
