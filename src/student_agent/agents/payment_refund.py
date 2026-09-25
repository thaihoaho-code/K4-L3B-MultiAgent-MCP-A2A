"""Payment/refund agent: reconcile captures and refunds for the case-scoped order record.

Verdict precedence (first match wins): refund failed, refund pending, refund completed, open
reconciliation mismatch, duplicate capture, reconciled. Several captures are a *valid split*
when they sum to the scoped order value; identical extra captures that exceed it are a
duplicate. The refund timeline is only queried when a claim or payment event points to a
refund, so non-refund cases do not spend a failing call.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..agent_types import PAYMENT_AGENT, AgentResult, AgentTask, Status
from ..evidence_cache import CaseEvidenceStore
from ..money import same_amount, to_brl, to_decimal
from ..scoping import ScopeWindow

REFUND_TOPICS = {"refund_pending", "refund_failed"}
REFUND_DONE = {"completed", "succeeded", "success", "refunded", "settled", "confirmed"}
REFUND_PENDING = {"pending", "requested", "processing", "open", "in_progress"}
REFUND_FAILED = {"failed", "rejected", "declined", "error", "reversed"}


def _events(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = data.get("events")
    return [row for row in data or [] if isinstance(row, dict)] if isinstance(data, list) else []


def _scoped(rows: list[dict[str, Any]], window: ScopeWindow | None) -> list[dict[str, Any]]:
    return rows if window is None else [row for row in rows if window.contains(row.get("event_at"))]


def pair_payments(
    payments: list[dict[str, Any]], captures: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Pair payment rows with capture events (position first, amount as a fallback)."""
    if len(payments) == len(captures):
        return list(zip(payments, captures, strict=True))
    remaining = list(captures)
    pairs = []
    for payment in payments:
        amount = to_decimal(payment.get("payment_value"))
        match = next(
            (
                event
                for event in remaining
                if same_amount(to_decimal(event.get("amount_brl")), amount)
            ),
            None,
        )
        if match is not None:
            remaining.remove(match)
        pairs.append((payment, match))
    return pairs


ISSUE_BY_VERDICT = {
    "refund_failed": "refund_failed",
    "refund_pending": "refund_pending",
    "capture_mismatch": "payment_mismatch",
    "duplicate_capture": "duplicate_charge",
}


def capture_groups(
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]],
) -> list[list[tuple[dict[str, Any], dict[str, Any] | None]]]:
    """Split paired payment rows into transactions: each ``payment_sequential == 1`` starts one."""
    groups: list[list[tuple[dict[str, Any], dict[str, Any] | None]]] = []
    for payment, event in pairs:
        if not groups or str(payment.get("payment_sequential")) == "1":
            groups.append([])
        groups[-1].append((payment, event))
    return groups


def issue_label(result: dict[str, Any]) -> str | None:
    if result["verdict"] in ISSUE_BY_VERDICT:
        return ISSUE_BY_VERDICT[result["verdict"]]
    return "valid_split_payment" if result["split"] else None


def analyze(
    captures: list[dict[str, Any]],
    other_events: list[dict[str, Any]],
    refunds: list[dict[str, Any]],
    order_value: Decimal | None,
    payment_types: list[str],
) -> dict[str, Any]:
    amounts = [to_decimal(event.get("amount_brl")) or Decimal(0) for event in captures]
    captured = sum(amounts, Decimal(0)) if captures else None
    refund_status = {str(event.get("status", "")).lower() for event in refunds}
    refunded = sum(
        (
            to_decimal(event.get("amount_brl")) or Decimal(0)
            for event in refunds
            if str(event.get("status", "")).lower() in REFUND_DONE
        ),
        Decimal(0),
    )
    mismatches = [
        event
        for event in other_events
        if "mismatch" in str(event.get("event_type", "")).lower()
        and str(event.get("status", "open")).lower() not in {"resolved", "closed"}
    ]
    split = duplicate = False
    duplicate_amount = Decimal(0)
    if len(amounts) >= 2:
        if order_value is not None and same_amount(captured, order_value):
            split = True
        elif len(set(amounts)) < len(amounts):
            duplicate = True
            duplicate_amount = sum(amounts, Decimal(0)) - sum(set(amounts), Decimal(0))
        elif len(set(payment_types)) > 1 and order_value is None:
            split = True

    if captured is None:
        verdict = "insufficient_evidence"
    elif refund_status & REFUND_FAILED:
        verdict = "refund_failed"
    elif refund_status & REFUND_PENDING:
        verdict = "refund_pending"
    elif refunds and refund_status <= REFUND_DONE:
        verdict = "refunded"
    elif mismatches:
        verdict = "capture_mismatch"
    elif duplicate:
        verdict = "duplicate_capture"
    else:
        verdict = "reconciled"
    refundable = max(captured - refunded, Decimal(0)) if captured is not None else None
    return {
        "verdict": verdict,
        "captured": captured,
        "refunded": refunded if captured is not None else None,
        "refundable": refundable,
        "split": split,
        "duplicate_amount": duplicate_amount,
        "mismatch_amount": sum(
            (to_decimal(event.get("amount_brl")) or Decimal(0) for event in mismatches), Decimal(0)
        ),
        "failed_refund_amount": sum(
            (
                to_decimal(event.get("amount_brl")) or Decimal(0)
                for event in refunds
                if str(event.get("status", "")).lower() in REFUND_FAILED
            ),
            Decimal(0),
        ),
        "pending_refund_amount": sum(
            (
                to_decimal(event.get("amount_brl")) or Decimal(0)
                for event in refunds
                if str(event.get("status", "")).lower() in REFUND_PENDING
            ),
            Decimal(0),
        ),
        "capture_count": len(captures),
    }


async def run_payment_refund_agent(task: AgentTask, store: CaseEvidenceStore) -> AgentResult:
    order_id = task.payload["order_id"]
    window: ScopeWindow | None = task.payload.get("window")
    order_value: Decimal | None = task.payload.get("order_value")
    topics = set(task.payload.get("claim_topics", []))
    warnings: list[str] = []
    refs: list[str] = []

    timeline = await store.fetch(PAYMENT_AGENT, "get_payment_timeline", order_id=order_id)
    data = timeline.data if timeline and isinstance(timeline.data, dict) else {}
    payments = [row for row in data.get("payments") or [] if isinstance(row, dict)]
    events = _events(data)
    if not data:
        fallback = await store.fetch(PAYMENT_AGENT, "get_order_payments", order_id=order_id)
        if fallback and isinstance(fallback.data, list):
            payments = [row for row in fallback.data if isinstance(row, dict)]
            refs.append(store.consume(PAYMENT_AGENT, fallback))
            warnings.append("payment_events_unavailable")
    else:
        refs.append(store.consume(PAYMENT_AGENT, timeline))

    all_captures = [
        event
        for event in events
        if str(event.get("event_type", "")).lower() == "captured"
        and str(event.get("status", "confirmed")).lower() == "confirmed"
    ]
    scoped_events = _scoped(events, window)
    scoped_captures = [event for event in all_captures if event in scoped_events]
    if not events and payments:
        scoped_captures = [
            {"amount_brl": row.get("payment_value"), "event_type": "captured"} for row in payments
        ]
        warnings.append("captures_from_payment_rows")
    pairs = pair_payments(payments, all_captures) if all_captures else [(p, None) for p in payments]
    scoped_payments = (
        [payment for payment, event in pairs if event is None or event in scoped_captures]
        if all_captures
        else payments
    )

    conflicts: list[dict[str, Any]] = []
    refund_events: list[dict[str, Any]] = []
    wants_refund = bool(topics & REFUND_TOPICS) or any(
        "refund" in str(event.get("event_type", "")).lower() for event in scoped_events
    )
    if wants_refund:
        refund = await store.fetch(PAYMENT_AGENT, "get_refund_timeline", order_id=order_id)
        rows = _events(refund.data if refund else None)
        refund_events = _scoped(rows, window)
        if refund and rows:
            refs.append(store.consume(PAYMENT_AGENT, refund))
        if not refund_events:
            warnings.append("no_refund_events_in_case_window")
    other_events = [event for event in scoped_events if event not in scoped_captures]
    groups = capture_groups(
        [
            (payment, event)
            for payment, event in pairs
            if event is not None and event in scoped_captures
        ]
    )
    if len(groups) > 1:
        # Several transactions share the case window: pick the one the evidence can explain.
        candidates = []
        for group in groups:
            captures = [event for _, event in group]
            outcome = analyze(
                captures,
                other_events,
                refund_events,
                order_value,
                [str(payment.get("payment_type")) for payment, _ in group],
            )
            candidates.append((group, outcome))
        chosen = next((g for g, o in candidates if issue_label(o) in topics), None)
        if chosen is None:
            chosen = next(
                (g for g, o in candidates if same_amount(o["captured"], order_value)), None
            )
        if chosen is not None:
            scoped_captures = [event for _, event in chosen]
            scoped_payments = [payment for payment, _ in chosen]
            warnings.append("multiple_transactions_in_case_window")
            conflicts.append(
                {
                    "field": "payment_transaction",
                    "sources": ["get_payment_timeline", "customer_claim"],
                    "preferred_source": None,
                    "rule": "ambiguous_transactions",
                }
            )
    result = analyze(
        scoped_captures,
        other_events,
        refund_events,
        order_value,
        [str(row.get("payment_type")) for row in scoped_payments],
    )
    references = list(
        dict.fromkeys(
            f"{order_id}:{row.get('payment_sequential')}"
            for row in scoped_payments
            if row.get("payment_sequential") not in (None, "")
        )
    )
    verdict = result["verdict"]
    ok = verdict != "insufficient_evidence"
    return AgentResult.reply(
        task,
        status=Status.OK if ok else Status.INSUFFICIENT_EVIDENCE,
        confidence=(0.7 if conflicts else 0.93) if ok else 0.3,
        decision_code="PAYMENT_ANALYZED" if ok else "PAYMENT_INSUFFICIENT_EVIDENCE",
        evidence_refs=[ref for ref in refs if ref],
        warnings=warnings,
        data={
            "payment_analysis": {
                "verdict": verdict,
                "captured_total_brl": to_brl(result["captured"]),
                "refunded_total_brl": to_brl(result["refunded"]),
                "refundable_total_brl": to_brl(result["refundable"]),
            },
            "payment_references": references,
            "facts": result,
            "refund_checked": wants_refund,
            "conflict_candidates": conflicts,
        },
    )
