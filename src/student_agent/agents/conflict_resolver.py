"""Conflict resolver: turn specialist conflict candidates into schema ``data_conflicts``.

Source precedence is field specific:

* order record fields -> the record inside the case window (customer history) beats a stale
  authoritative row from another period;
* delivery timestamps -> the case-scoped timeline beats the shipment summary header;
* delay attribution -> authoritative timeline (handoff limit vs carrier date) beats events;
* anything without a precedence rule stays unresolved (``selected_source = null``).

The resolver also enforces responsibility consistency: a seller can only be responsible when
seller evidence supports it; otherwise the party ID is cleared and a warning is returned.
"""

from __future__ import annotations

from typing import Any

from ..agent_types import CONFLICT_RESOLVER, AgentResult, AgentTask, Status
from ..evidence_cache import CaseEvidenceStore

MAX_CONFLICTS = 5
PRECEDENCE: dict[str, tuple[str, ...]] = {
    "order_purchase_timestamp": ("get_customer_history", "get_order"),
    "order_status": ("get_customer_history", "get_order"),
    "order_delivered_customer_date": ("get_customer_history", "get_order"),
    "delivered_customer_at": ("get_customer_history", "get_shipment_summary"),
    "delivery_delay_attribution": ("get_customer_history", "get_shipment_summary"),
}
RESOLUTION_CODES = {
    "case_window_record": "CASE_WINDOW_RECORD_SELECTED",
    "authoritative_timeline": "AUTHORITATIVE_TIMELINE_SELECTED",
}


def resolve(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    conflicts: list[dict[str, Any]] = []
    unresolved = 0
    seen: set[str] = set()
    for candidate in candidates:
        field = str(candidate.get("field", ""))[:100]
        sources = list(dict.fromkeys(str(source)[:80] for source in candidate.get("sources", [])))
        if not field or len(sources) < 2 or field in seen:
            continue
        seen.add(field)
        ranking = PRECEDENCE.get(field, ())
        preferred = candidate.get("preferred_source")
        selected = next((source for source in ranking if source in sources), None)
        if selected is None or (preferred and preferred != selected):
            selected, code = None, "UNRESOLVED_INSUFFICIENT_EVIDENCE"
            unresolved += 1
        else:
            code = RESOLUTION_CODES.get(str(candidate.get("rule")), "SOURCE_PRECEDENCE_APPLIED")
        conflicts.append(
            {
                "field": field,
                "sources": sources[:5],
                "selected_source": selected,
                "resolution_code": code,
            }
        )
        if len(conflicts) == MAX_CONFLICTS:
            break
    return conflicts, unresolved


def check_responsibility(
    parties: list[dict[str, Any]], seller_ids: list[str], shipment_verdict: str | None, issue: str
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings = []
    checked = []
    for party in parties:
        party = dict(party)
        if party["party_type"] == "seller" and party.get("party_id") not in seller_ids:
            warnings.append("seller_party_not_in_affected_sellers")
            party["party_id"] = None
        if (
            issue == "late_delivery_logistics"
            and party["party_type"] == "seller"
            and shipment_verdict != "seller_delay"
        ):
            warnings.append("seller_blamed_without_seller_delay")
            continue
        checked.append(party)
    return checked or [{"party_type": "unknown", "party_id": None}], warnings


async def run_conflict_resolver(task: AgentTask, store: CaseEvidenceStore) -> AgentResult:
    payload = task.payload
    conflicts, unresolved = resolve(payload.get("conflict_candidates", []))
    parties, warnings = check_responsibility(
        payload.get("responsible_parties", []),
        payload.get("seller_ids", []),
        payload.get("shipment_verdict"),
        payload.get("primary_issue", ""),
    )
    decision = (
        "CONFLICT_UNRESOLVED"
        if unresolved
        else ("CONFLICT_RESOLVED" if conflicts else "NO_CONFLICT")
    )
    refs = [ref for ref in payload.get("supporting_refs", []) if store.owns(ref)]
    if conflicts:
        store.trace.emit(
            case_id=task.case_id,
            event_type="policy_decided",
            actor=CONFLICT_RESOLVER,
            target="coordinator",
            decision_code=decision,
            attributes={"conflict_count": len(conflicts), "unresolved_count": unresolved},
        )
    return AgentResult.reply(
        task,
        status=Status.OK if not unresolved else Status.NEEDS_FOLLOWUP,
        confidence=0.9 if not unresolved else 0.6,
        decision_code=decision,
        evidence_refs=refs,
        warnings=warnings,
        data={
            "data_conflicts": conflicts,
            "responsible_parties": parties,
            "unresolved": unresolved,
        },
    )
