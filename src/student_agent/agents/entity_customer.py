"""Entity/customer agent: resolve the order in scope and the customer context.

Candidates are ranked with deterministic signals (customer-history membership, exact or fuzzy
ID match, claimed order, ID shape, authoritative order lookup). Only one ``get_order`` lookup
is spent per plausible candidate and malformed decoys are rejected without any MCP call.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from ..agent_types import ENTITY_AGENT, AgentResult, AgentTask, Status
from ..evidence_cache import CaseEvidenceStore
from ..scoping import PURCHASE, select_scoped_record

ORDER_ID_SHAPE = re.compile(r"^[0-9a-f]{32}$")
FUZZY_THRESHOLD = 0.9
RESOLVE_THRESHOLD = 0.6
RESOLVE_GAP = 0.25
AMBIGUOUS_FLOOR = 0.4
MAX_ORDER_LOOKUPS = 2
CONFLICT_FIELDS = ("order_purchase_timestamp", "order_status", "order_delivered_customer_date")

WEIGHTS = {
    "history_exact": 0.35,
    "history_fuzzy": 0.25,
    "claimed_order": 0.2,
    "well_formed_id": 0.1,
    "order_verified": 0.3,
}


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"[^0-9a-z]", "", text)


def similarity(left: Any, right: Any) -> float:
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class Candidate:
    raw: str
    canonical: str
    signals: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return round(min(sum(WEIGHTS[name] for name in set(self.signals)), 1.0), 4)


def _history_orders(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    return [row for row in data.get("orders") or [] if isinstance(row, dict)]


def rank_candidates(
    candidate_ids: list[str], claimed: str | None, history_ids: list[str]
) -> list[Candidate]:
    ranked: list[Candidate] = []
    seen: set[str] = set()
    for raw in candidate_ids:
        key = normalize(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        candidate = Candidate(raw=raw, canonical=raw.strip())
        exact = next((oid for oid in history_ids if normalize(oid) == key), None)
        if exact:
            candidate.canonical = exact
            candidate.signals.append("history_exact")
        else:
            best = max(history_ids, key=lambda oid: similarity(oid, raw), default=None)
            if best and similarity(best, raw) >= FUZZY_THRESHOLD:
                candidate.canonical = best
                candidate.signals.append("history_fuzzy")
        if claimed and normalize(claimed) == key:
            candidate.signals.append("claimed_order")
        if ORDER_ID_SHAPE.fullmatch(candidate.canonical):
            candidate.signals.append("well_formed_id")
        ranked.append(candidate)
    return sorted(ranked, key=lambda item: (-item.score, candidate_ids.index(item.raw)))


def _conflict_candidates(authoritative: dict[str, Any], scoped: dict[str, Any]) -> list[dict]:
    conflicts = []
    for name in CONFLICT_FIELDS:
        left, right = authoritative.get(name), scoped.get(name)
        if left != right:
            conflicts.append(
                {
                    "field": name,
                    "sources": ["get_order", "get_customer_history"],
                    "preferred_source": "get_customer_history",
                    "rule": "case_window_record",
                }
            )
    return conflicts


async def run_entity_customer_agent(task: AgentTask, store: CaseEvidenceStore) -> AgentResult:
    payload = task.payload
    claimed = payload.get("claimed_order_id")
    candidate_ids = [
        str(item) for item in [claimed, *payload.get("candidate_order_ids", [])] if item
    ]
    hint = payload.get("customer_unique_id_hint")
    warnings: list[str] = []
    history_ref: str | None = None
    order_ref: str | None = None

    history = None
    if hint:
        history = await store.fetch(ENTITY_AGENT, "get_customer_history", customer_unique_id=hint)
        if history is None:
            warnings.append("customer_history_unavailable")
    history_rows = _history_orders(history.data if history else None)
    history_ids = list(
        dict.fromkeys(str(row["order_id"]) for row in history_rows if row.get("order_id"))
    )
    if history and history_rows:
        history_ref = store.consume(ENTITY_AGENT, history)

    ranked = rank_candidates(candidate_ids, claimed, history_ids)
    # Verify the leader and every close contender (never a malformed decoy) so ties stay ties.
    lead = ranked[0].score if ranked else 0.0
    orders: dict[str, Any] = {}
    lookups = 0
    for candidate in ranked:
        if lookups >= MAX_ORDER_LOOKUPS or "well_formed_id" not in candidate.signals:
            continue
        if lookups and candidate.score < lead - RESOLVE_GAP:
            break
        lookups += 1
        order = await store.fetch(ENTITY_AGENT, "get_order", order_id=candidate.canonical)
        if order is not None and isinstance(order.data, dict):
            candidate.signals.append("order_verified")
            orders[candidate.canonical] = order
    ranked.sort(key=lambda item: -item.score)

    top = ranked[0] if ranked else None
    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    if top and top.score >= RESOLVE_THRESHOLD and top.score - runner_up >= RESOLVE_GAP:
        status = "resolved"
    elif top and top.score >= AMBIGUOUS_FLOOR:
        status = "ambiguous"
    else:
        status = "not_found"
    resolved = [top.canonical] if status == "resolved" and top else []
    authoritative: dict[str, Any] | None = None
    if resolved and resolved[0] in orders:
        authoritative = orders[resolved[0]].data
        order_ref = store.consume(ENTITY_AGENT, orders[resolved[0]])
    rejected = [item.raw for item in ranked if status == "resolved" and item is not top]

    customer_verified = bool(resolved and history and resolved[0] in history_ids)
    customer_unique_id = None
    if customer_verified and isinstance(history.data, dict):
        customer_unique_id = history.data.get("customer_unique_id") or hint
    related = history_ids if customer_verified else []

    scoped = None
    if resolved:
        rows = [row for row in history_rows if row.get("order_id") == resolved[0]]
        if authoritative:
            rows.append(authoritative)
        scoped = select_scoped_record(rows, payload.get("opened_at"))
        if scoped is None:
            warnings.append("no_order_record_before_case_opened")

    conflicts = []
    if scoped and authoritative and authoritative.get(PURCHASE) != scoped.row.get(PURCHASE):
        conflicts = _conflict_candidates(authoritative, scoped.row)

    if status == "resolved":
        confidence = top.score
        if scoped is None:
            confidence = min(confidence, 0.6)
        decision = "ENTITY_RESOLVED"
        result_status = Status.OK
    elif status == "ambiguous":
        confidence = 0.45
        decision = "ENTITY_AMBIGUOUS"
        result_status = Status.AMBIGUOUS
    else:
        confidence = 0.2
        decision = "ENTITY_NOT_FOUND"
        result_status = Status.INSUFFICIENT_EVIDENCE

    return AgentResult.reply(
        task,
        status=result_status,
        confidence=confidence,
        decision_code=decision,
        evidence_refs=[ref for ref in (history_ref, order_ref) if ref],
        warnings=warnings,
        data={
            "status": status,
            "resolved_order_ids": resolved,
            "rejected_candidates": list(dict.fromkeys(rejected)),
            "customer_unique_id": customer_unique_id,
            "related_order_ids": related,
            "candidate_scores": [
                {"candidate_id": item.raw, "score": item.score, "matched_signals": item.signals}
                for item in ranked
            ],
            "scoped": scoped,
            "authoritative_row": authoritative,
            "history_ref": history_ref,
            "order_ref": order_ref,
            "conflict_candidates": conflicts,
        },
    )
