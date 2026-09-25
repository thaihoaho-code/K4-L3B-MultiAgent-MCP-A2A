"""Shipment agent: classify the delivery timeline of the case-scoped order record.

Timeline precedence: the case-scoped order record (customer history) supplies purchase,
carrier handoff, delivery and estimate; the shipment summary supplies per-seller handoff limits
and shipment events. A seller is late when the carrier handoff happened after its shipping
limit; otherwise a late delivery is attributed to logistics. Shipment events corroborate the
timeline. When they disagree the timeline stays authoritative and a conflict is surfaced for
the conflict resolver instead of being decided here.
"""

from __future__ import annotations

from typing import Any

from ..agent_types import SHIPMENT_AGENT, AgentResult, AgentTask, Status
from ..evidence_cache import CaseEvidenceStore
from ..scoping import ScopeWindow, is_late, parse_ts, timeline_complete

EVENT_VERDICTS = {"lost": "lost", "returned": "returned", "return": "returned"}
ACTOR_VERDICTS = {"seller": "seller_delay", "logistics_provider": "logistics_delay"}
NOT_SHIPPED = {"canceled", "unavailable", "created", "invoiced", "processing", "approved"}


def _scoped(rows: list[dict[str, Any]], key: str, window: ScopeWindow | None) -> list[dict]:
    if window is None:
        return rows
    inside = [row for row in rows if window.contains(row.get(key))]
    return inside


def event_verdict(events: list[dict[str, Any]]) -> str | None:
    confirmed = [event for event in events if event.get("status", "confirmed") == "confirmed"]
    for event in confirmed:
        kind = str(event.get("event_type", "")).lower()
        for token, verdict in EVENT_VERDICTS.items():
            if token in kind:
                return verdict
    for event in confirmed:
        if "late" in str(event.get("event_type", "")).lower():
            return ACTOR_VERDICTS.get(str(event.get("actor")))
    return None


def classify(
    row: dict[str, Any], limits: list[dict[str, Any]], events: list[dict[str, Any]]
) -> dict[str, Any]:
    carrier = parse_ts(row.get("order_delivered_carrier_date"))
    late_sellers = []
    for limit in limits:
        deadline = parse_ts(limit.get("shipping_limit_at"))
        if carrier and deadline and carrier > deadline and limit.get("seller_id"):
            late_sellers.append(str(limit["seller_id"]))
    late_sellers = list(dict.fromkeys(late_sellers))
    late = is_late(row)
    status = str(row.get("order_status") or "")

    if late is None:
        timeline = "insufficient_evidence"
    elif late:
        timeline = "seller_delay" if late_sellers else "logistics_delay"
    else:
        timeline = "on_time"
    from_events = event_verdict(events)

    conflict = None
    verdict = timeline
    events_only = timeline == "insufficient_evidence" and status not in NOT_SHIPPED
    if from_events in {"lost", "returned"} or (from_events and events_only):
        verdict = from_events
    elif from_events and timeline != "insufficient_evidence" and from_events != timeline:
        conflict = {
            "field": "delivery_delay_attribution",
            "sources": ["get_customer_history", "get_shipment_summary"],
            "preferred_source": "get_customer_history",
            "rule": "authoritative_timeline",
        }
    if verdict == "seller_delay" and not late_sellers:
        late_sellers = list(
            dict.fromkeys(str(limit["seller_id"]) for limit in limits if limit.get("seller_id"))
        )
    return {
        "verdict": verdict,
        "timeline_verdict": timeline,
        "event_verdict": from_events,
        "late_seller_ids": late_sellers if verdict == "seller_delay" else [],
        "timeline_complete": timeline_complete(row) and late is not None,
        "conflict": conflict,
    }


async def run_shipment_agent(task: AgentTask, store: CaseEvidenceStore) -> AgentResult:
    order_id = task.payload["order_id"]
    row: dict[str, Any] = task.payload.get("scoped_row") or {}
    window: ScopeWindow | None = task.payload.get("window")
    warnings: list[str] = []
    refs: list[str] = []

    summary = await store.fetch(SHIPMENT_AGENT, "get_shipment_summary", order_id=order_id)
    data = summary.data if summary and isinstance(summary.data, dict) else {}
    if data:
        refs.append(store.consume(SHIPMENT_AGENT, summary))
    else:
        warnings.append("shipment_summary_unavailable")
    limits = [item for item in data.get("shipping_limits") or [] if isinstance(item, dict)]
    events = [item for item in data.get("events") or [] if isinstance(item, dict)]
    scoped_limits = _scoped(limits, "shipping_limit_at", window) or limits
    scoped_events = _scoped(events, "event_at", window)

    if not row and data:
        row = {
            "order_status": data.get("order_status"),
            "order_delivered_carrier_date": data.get("delivered_carrier_at"),
            "order_delivered_customer_date": data.get("delivered_customer_at"),
            "order_estimated_delivery_date": data.get("estimated_delivery_at"),
        }
        warnings.append("timeline_from_shipment_summary")
    result = classify(row, scoped_limits, scoped_events)

    conflicts = []
    if result["conflict"]:
        conflicts.append(result["conflict"])
    if (
        data
        and row
        and data.get("delivered_customer_at") != row.get("order_delivered_customer_date")
    ):
        conflicts.append(
            {
                "field": "delivered_customer_at",
                "sources": ["get_shipment_summary", "get_customer_history"],
                "preferred_source": "get_customer_history",
                "rule": "case_window_record",
            }
        )

    verdict = result["verdict"]
    confident = verdict not in {"insufficient_evidence", "conflicting"}
    confidence = 0.92 if confident and result["timeline_complete"] else (0.7 if confident else 0.35)
    if result["conflict"]:
        confidence = min(confidence, 0.75)
    return AgentResult.reply(
        task,
        status=Status.OK if confident else Status.INSUFFICIENT_EVIDENCE,
        confidence=confidence,
        decision_code="SHIPMENT_ANALYZED" if confident else "SHIPMENT_INSUFFICIENT_EVIDENCE",
        evidence_refs=[ref for ref in refs if ref],
        warnings=warnings,
        data={
            "shipment_analysis": {
                "verdict": verdict,
                "late_seller_ids": result["late_seller_ids"],
                "timeline_complete": result["timeline_complete"],
            },
            "timeline_verdict": result["timeline_verdict"],
            "event_verdict": result["event_verdict"],
            "scoped_event_count": len(scoped_events),
            "limit_seller_ids": list(
                dict.fromkeys(
                    str(item["seller_id"]) for item in scoped_limits if item.get("seller_id")
                )
            ),
            "conflict_candidates": conflicts,
        },
    )
