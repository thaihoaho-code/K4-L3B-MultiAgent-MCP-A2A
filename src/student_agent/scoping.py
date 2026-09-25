"""Temporal case scoping.

The MCP sources can hold several records for the same order ID (for example a stale
authoritative order row plus the customer-history row that matches the complaint). The record
in scope for a case is the most recent order record purchased at or before ``opened_at`` whose
outcome was already due (estimated delivery not after ``opened_at``); its
evidence window runs from that purchase until the next record's purchase (open-ended when it
is the latest). Timestamped rows (items, payment/refund/shipment events) are kept only when
they fall inside the window.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

PURCHASE = "order_purchase_timestamp"


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


@dataclass(frozen=True)
class ScopeWindow:
    start: datetime
    end: datetime | None

    def contains(self, value: Any) -> bool:
        moment = parse_ts(value)
        if moment is None:
            return False
        return moment >= self.start and (self.end is None or moment < self.end)


@dataclass(frozen=True)
class ScopedRecord:
    row: dict[str, Any]
    window: ScopeWindow
    record_count: int
    out_of_scope: tuple[dict[str, Any], ...]


def _unique_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = (row.get("order_id"), row.get(PURCHASE), row.get("order_status"))
        if key in seen or parse_ts(row.get(PURCHASE)) is None:
            continue
        seen.add(key)
        result.append(dict(row))
    return result


TERMINAL_STATUSES = {"canceled", "unavailable"}


def _outcome_due(row: Mapping[str, Any], opened: datetime | None) -> bool:
    if opened is None:
        return True
    estimated = parse_ts(row.get("order_estimated_delivery_date"))
    if estimated is not None:
        return estimated <= opened
    return str(row.get("order_status")) in TERMINAL_STATUSES


def select_scoped_record(rows: Iterable[Mapping[str, Any]], opened_at: Any) -> ScopedRecord | None:
    """Pick the order record in scope for a case opened at ``opened_at``."""
    candidates = sorted(_unique_rows(rows), key=lambda row: parse_ts(row[PURCHASE]))
    if not candidates:
        return None
    opened = parse_ts(opened_at)
    eligible = [row for row in candidates if opened is None or parse_ts(row[PURCHASE]) <= opened]
    if not eligible:
        return None
    # A complaint can only concern a record whose outcome was already due when the case opened.
    due = [row for row in eligible if _outcome_due(row, opened)]
    chosen = (due or eligible)[-1]
    start = parse_ts(chosen[PURCHASE])
    later = [parse_ts(row[PURCHASE]) for row in candidates if parse_ts(row[PURCHASE]) > start]
    window = ScopeWindow(start=start, end=min(later) if later else None)
    others = tuple(row for row in candidates if row is not chosen)
    return ScopedRecord(
        row=chosen, window=window, record_count=len(candidates), out_of_scope=others
    )


def is_late(row: Mapping[str, Any]) -> bool | None:
    delivered = parse_ts(row.get("order_delivered_customer_date"))
    estimated = parse_ts(row.get("order_estimated_delivery_date"))
    if delivered is None or estimated is None:
        return None
    return delivered > estimated


TIMELINE_FIELDS = (
    PURCHASE,
    "order_approved_at",
    "order_delivered_carrier_date",
    "order_delivered_customer_date",
    "order_estimated_delivery_date",
)


def timeline_complete(row: Mapping[str, Any]) -> bool:
    return all(parse_ts(row.get(name)) is not None for name in TIMELINE_FIELDS)
