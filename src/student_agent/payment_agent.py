from __future__ import annotations

import inspect
import math
import re
from collections import Counter
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .state import EntityResult, PaymentResult
from .trace import TraceWriter

_ACTOR = "payment_agent"
_EVIDENCE_REF = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")
_CENT = Decimal("0.01")
_ZERO = Decimal("0")

_PAYMENT_TOOL_PREFERENCES = (
    "get_payment_timeline",
    "get_order_payments",
    "get_payment_details",
    "get_payment_history",
    "get_payment_records",
    "get_payment",
    "check_payment",
)
_REFUND_TOOL_PREFERENCES = (
    "get_refund_timeline",
    "get_refund_status",
    "get_refund_details",
    "get_refund_records",
    "get_refunds",
    "check_refund",
)

_PAYMENT_CONTAINERS = (
    "payments",
    "payment_records",
    "captures",
    "capture_records",
    "transactions",
    "transaction_records",
    "payment_events",
    "payment_timeline",
    "lifecycle_events",
    "events",
    "ledger",
    "records",
    "data",
)
_REFUND_CONTAINERS = (
    "refunds",
    "refund_records",
    "refund_events",
    "refund_timeline",
    "lifecycle_events",
    "events",
    "transactions",
    "records",
    "data",
)

_CAPTURE_TOTAL_KEYS = (
    "captured_total_brl",
    "captured_total",
    "total_captured_brl",
    "total_captured",
)
_REFUNDED_TOTAL_KEYS = (
    "refunded_total_brl",
    "refunded_total",
    "total_refunded_brl",
    "total_refunded",
)
_EXPECTED_TOTAL_KEYS = (
    "expected_total_brl",
    "order_total_brl",
    "invoice_total_brl",
    "grand_total_brl",
    "total_order_brl",
    "order_total",
    "invoice_total",
    "total",
)

_REFUND_TOPICS = frozenset(
    {
        "canceled_order_paid",
        "duplicate_charge",
        "refund_failed",
        "refund_pending",
        "requested_full_refund",
        "unavailable_order_paid",
    }
)


def _normalise_field(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _first_value(record: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    normalised = {_normalise_field(str(key)): value for key, value in record.items()}
    for key in keys:
        value = normalised.get(_normalise_field(key))
        if value is not None:
            return value
    return None


def _money(value: Any) -> Decimal | None:
    """Parse common BRL representations without allowing NaN or infinity."""

    if value is None or isinstance(value, (bool, Mapping, list, tuple)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        text = value.strip().replace("R$", "").replace("BRL", "").strip()
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        text = re.sub(r"[^0-9.\-]", "", text)
        if text in {"", ".", "-", "-."}:
            return None
        value = text
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _nonnegative(value: Decimal | None) -> Decimal | None:
    return None if value is None else max(value, _ZERO)


def _as_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _text(value: Any) -> str:
    return str(value).strip().lower() if value is not None else ""


def _walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def _find_money(value: Any, keys: tuple[str, ...]) -> Decimal | None:
    for mapping in _walk_mappings(value):
        parsed = _money(_first_value(mapping, keys))
        if parsed is not None:
            return _nonnegative(parsed)
    return None


def _looks_like_record(value: Mapping[str, Any]) -> bool:
    fields = {_normalise_field(str(key)) for key in value}
    markers = {
        "amount",
        "amount_brl",
        "captured_amount",
        "captured_amount_brl",
        "captured_total",
        "event_type",
        "id",
        "is_duplicate",
        "payment_id",
        "payment_value",
        "payment_reference",
        "refund_status",
        "status",
        "transaction_id",
        "transaction_reference",
        "type",
    }
    return bool(fields & markers)


def _collect_records(value: Any, containers: tuple[str, ...]) -> list[dict[str, Any]]:
    """Extract payment/refund records from the supported nested evidence shapes."""

    if isinstance(value, list):
        records: list[dict[str, Any]] = []
        for child in value:
            records.extend(_collect_records(child, containers))
        return records
    if not isinstance(value, Mapping):
        return []

    nested: list[dict[str, Any]] = []
    found_container = False
    for container in containers:
        child = _first_value(value, (container,))
        if child is not None:
            found_container = True
            nested.extend(_collect_records(child, containers))
    if found_container:
        return nested
    if _looks_like_record(value):
        return [dict(value)]
    for child in value.values():
        nested.extend(_collect_records(child, containers))
    return nested


def _record_status(record: Mapping[str, Any]) -> str:
    return _text(
        _first_value(
            record,
            (
                "status",
                "payment_status",
                "capture_status",
                "refund_status",
                "state",
                "result",
                "event_type",
                "event",
            ),
        )
    ).replace("-", "_").replace(" ", "_")


def _record_reference(record: Mapping[str, Any]) -> str | None:
    value = _first_value(
        record,
        (
            "payment_reference",
            "payment_ref",
            "transaction_reference",
            "transaction_id",
            "capture_id",
            "payment_id",
            "reference",
            "id",
        ),
    )
    if value is None or isinstance(value, (Mapping, list, tuple)):
        return None
    reference = str(value).strip()
    return reference or None


def _capture_amount(record: Mapping[str, Any]) -> Decimal | None:
    kind = _text(_first_value(record, ("type", "kind", "category", "domain")))
    if "refund" in kind:
        return None

    captured_flag = _first_value(record, ("captured", "is_captured"))
    if captured_flag is False or _text(captured_flag) in {"false", "no", "0"}:
        return None

    status = _record_status(record)
    if status in {"failed", "declined", "rejected", "pending", "authorized", "void", "canceled"}:
        return None

    amount = _first_value(
        record,
        (
            "captured_amount_brl",
            "captured_amount",
            "capture_amount_brl",
            "capture_amount",
            "payment_value_brl",
            "payment_value",
            "amount_brl",
            "amount",
            "value_brl",
            "value",
        ),
    )
    return _nonnegative(_money(amount))


def _refund_amount(record: Mapping[str, Any]) -> Decimal | None:
    amount = _first_value(
        record,
        (
            "refunded_amount_brl",
            "refund_amount_brl",
            "refunded_amount",
            "refund_amount",
            "amount_brl",
            "amount",
            "value_brl",
            "value",
        ),
    )
    return _nonnegative(_money(amount))


def _sum_capture(data: Any, records: list[dict[str, Any]]) -> Decimal | None:
    direct = _find_money(data, _CAPTURE_TOTAL_KEYS)
    if direct is not None:
        return direct
    amounts = [
        amount
        for record in records
        if (amount := _capture_amount(record)) is not None
    ]
    return sum(amounts, _ZERO) if amounts else None


def _is_completed_refund(record: Mapping[str, Any]) -> bool:
    status = _record_status(record)
    if not status:
        return True
    return status in {
        "completed",
        "complete",
        "refunded",
        "settled",
        "success",
        "succeeded",
        "processed",
    }


def _sum_refunds(data: Any, records: list[dict[str, Any]]) -> Decimal | None:
    direct = _find_money(data, _REFUNDED_TOTAL_KEYS)
    if direct is not None:
        return direct
    amounts = [
        amount
        for record in records
        if _is_completed_refund(record)
        and (amount := _refund_amount(record)) is not None
    ]
    return sum(amounts, _ZERO) if amounts else _ZERO


def _refund_state(data: Any, records: list[dict[str, Any]]) -> str | None:
    statuses = {_record_status(record) for record in records}
    if isinstance(data, Mapping):
        root_status = _text(_first_value(data, ("status", "refund_status", "state")))
        if root_status:
            statuses.add(root_status.replace("-", "_").replace(" ", "_"))

    if statuses & {"failed", "failure", "declined", "rejected", "error"}:
        return "refund_failed"
    if statuses & {"pending", "processing", "initiated", "requested", "in_progress"}:
        return "refund_pending"
    if statuses & {
        "completed",
        "complete",
        "refunded",
        "settled",
        "success",
        "succeeded",
        "processed",
    }:
        return "refunded"
    return None


def _references(data: Any, records: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for record in records:
        if (reference := _record_reference(record)) is not None:
            values.append(reference)
    for mapping in _walk_mappings(data):
        value = _first_value(mapping, ("payment_references", "payment_refs", "references"))
        if isinstance(value, list):
            values.extend(str(item).strip() for item in value if str(item).strip())
        elif value is not None and str(value).strip():
            values.append(str(value).strip())
    return list(dict.fromkeys(values))[:20]


def _duplicate_capture(records: list[dict[str, Any]], data: Any) -> bool:
    for mapping in _walk_mappings(data):
        flag = _first_value(mapping, ("duplicate_capture", "is_duplicate", "duplicate"))
        if flag is True or _text(flag) in {"true", "yes", "1", "duplicate", "duplicated"}:
            return True

    captured_records = [record for record in records if _capture_amount(record) is not None]
    references = [
        reference
        for record in captured_records
        if (reference := _record_reference(record)) is not None
    ]
    if any(count > 1 for count in Counter(references).values()):
        return True
    statuses = {_record_status(record) for record in captured_records}
    return bool(statuses & {"duplicate", "duplicated", "duplicate_capture"})


def _tool_score(name: str, category: str) -> int:
    normalised = _normalise_field(name)
    if category == "refund":
        if "refund" not in normalised:
            return -1
        score = 10
        if "status" in normalised:
            score += 4
        if "detail" in normalised or "record" in normalised:
            score += 2
        return score

    if "refund" in normalised:
        return -1
    markers = ("payment", "capture", "transaction", "ledger", "charge")
    if not any(marker in normalised for marker in markers):
        return -1
    score = 5
    if "payment" in normalised:
        score += 4
    if "detail" in normalised or "record" in normalised or "history" in normalised:
        score += 2
    return score


def _select_tool(available: list[str] | None, category: str) -> str | None:
    default = "get_refund_status" if category == "refund" else "get_payment_details"
    preferences = (
        _REFUND_TOOL_PREFERENCES if category == "refund" else _PAYMENT_TOOL_PREFERENCES
    )
    if available is None:
        return default
    if not available:
        return None

    normalised = {_normalise_field(name): name for name in available}
    for preferred in preferences:
        if preferred in normalised:
            return normalised[preferred]

    candidates = [
        (score, name)
        for name in available
        if (score := _tool_score(name, category)) >= 0
    ]
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


async def _available_tools(gateway: EvidenceGateway) -> list[str] | None:
    method = getattr(gateway, "list_tools", None)
    if method is None:
        return None
    try:
        result = method()
        if inspect.isawaitable(result):
            result = await result
    except Exception:  # noqa: BLE001 - missing discovery is handled as missing evidence
        return []
    return [str(name) for name in (result or [])]


def _evidence_data(evidence: Any) -> Any:
    if isinstance(evidence, Mapping) and "data" in evidence:
        return evidence["data"]
    return evidence


def _evidence_ref(evidence: Any) -> str | None:
    if not isinstance(evidence, Mapping):
        return None
    value = evidence.get("evidence_ref")
    if isinstance(value, str) and _EVIDENCE_REF.fullmatch(value):
        return value
    return None


def _case_requires_refund(case: Mapping[str, Any] | None) -> bool:
    if case is None:
        return False
    if case.get("refund_required") is True or case.get("force_refund") is True:
        return True

    request = case.get("customer_request")
    if not isinstance(request, Mapping):
        return False
    claims = request.get("claims")
    topics = {
        _normalise_field(str(claim.get("topic")))
        for claim in claims or []
        if isinstance(claim, Mapping) and claim.get("topic")
    }
    if topics & _REFUND_TOPICS:
        return True
    message = _text(request.get("message"))
    return any(token in message for token in ("refund", "reimburse", "hoàn", "hoan"))


def _resolve_case_id(
    case_id: str | None,
    case: Mapping[str, Any] | None,
    entity: EntityResult,
    trace: TraceWriter,
) -> str | None:
    candidates: list[Any] = [case_id]
    if case is not None:
        candidates.append(case.get("case_id"))
    candidates.extend(
        [
            getattr(entity, "case_id", None),
            getattr(trace, "case_id", None),
            getattr(trace, "current_case_id", None),
        ]
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _expected_total(
    explicit_total: Any,
    entity: EntityResult,
    case: Mapping[str, Any] | None,
    payment_data: Any,
) -> Decimal | None:
    direct = _nonnegative(_money(explicit_total))
    if direct is not None:
        return direct

    for attribute in ("expected_total_brl", "order_total_brl", "invoice_total_brl"):
        direct = _nonnegative(_money(getattr(entity, attribute, None)))
        if direct is not None:
            return direct

    if case is not None:
        order = case.get("order")
        direct = _find_money(order, _EXPECTED_TOTAL_KEYS)
        if direct is not None:
            return direct
        direct = _find_money(case, _EXPECTED_TOTAL_KEYS)
        if direct is not None:
            return direct

    return _find_money(payment_data, _EXPECTED_TOTAL_KEYS)


def _insufficient(evidence_refs: list[str] | None = None) -> PaymentResult:
    return PaymentResult(
        verdict="insufficient_evidence",
        captured_total_brl=None,
        refunded_total_brl=None,
        refundable_total_brl=None,
        payment_references=[],
        evidence_refs=list(dict.fromkeys(evidence_refs or []))[:20],
    )


class PaymentAgent:
    """Investigate captured payments and refunds for the resolved order."""

    async def check_transactions(
        self,
        entity: EntityResult,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        *,
        case_id: str | None = None,
        case: Mapping[str, Any] | None = None,
        expected_total_brl: float | int | str | Decimal | None = None,
        refund_required: bool | None = None,
    ) -> PaymentResult:
        """Reconcile payment/refund evidence without inventing missing values.

        ``case_id`` and ``case`` are optional for backwards compatibility with
        the starter signature. Production callers should pass both so every MCP
        request is correlated to the correct case and refund lookups stay scoped.
        """

        order_id = entity.resolved_order_id
        if not isinstance(order_id, str) or not order_id.strip():
            return _insufficient()

        effective_case_id = _resolve_case_id(case_id, case, entity, trace)
        if effective_case_id is None:
            return _insufficient()

        available = await _available_tools(gateway)
        payment_tool = _select_tool(available, "payment")
        if payment_tool is None:
            return _insufficient()

        evidence_refs: list[str] = []
        try:
            payment_evidence = await gateway.call(
                payment_tool,
                case_id=effective_case_id,
                order_id=order_id.strip(),
            )
        except Exception:  # noqa: BLE001 - missing payment evidence is a valid outcome
            return _insufficient()

        if (payment_ref := _evidence_ref(payment_evidence)) is not None:
            evidence_refs.append(payment_ref)
            trace.emit(
                case_id=effective_case_id,
                event_type="tool_result_consumed",
                actor=_ACTOR,
                tool_name=payment_tool,
                evidence_refs=[payment_ref],
            )

        payment_data = _evidence_data(payment_evidence)
        payment_records = _collect_records(payment_data, _PAYMENT_CONTAINERS)
        captured = _sum_capture(payment_data, payment_records)
        references = _references(payment_data, payment_records)
        duplicate = _duplicate_capture(payment_records, payment_data)
        expected = _expected_total(expected_total_brl, entity, case, payment_data)

        if refund_required is None:
            refund_required = _case_requires_refund(case)

        refunded: Decimal | None = _ZERO if not refund_required else None
        refund_state: str | None = None
        if refund_required:
            refund_tool = _select_tool(available, "refund")
            if refund_tool is not None:
                try:
                    refund_evidence = await gateway.call(
                        refund_tool,
                        case_id=effective_case_id,
                        order_id=order_id.strip(),
                    )
                except Exception:  # noqa: BLE001 - preserve payment evidence on refund failure
                    refund_evidence = None
                if refund_evidence is not None:
                    if (refund_ref := _evidence_ref(refund_evidence)) is not None:
                        evidence_refs.append(refund_ref)
                        trace.emit(
                            case_id=effective_case_id,
                            event_type="tool_result_consumed",
                            actor=_ACTOR,
                            tool_name=refund_tool,
                            evidence_refs=[refund_ref],
                        )
                    refund_data = _evidence_data(refund_evidence)
                    refund_records = _collect_records(refund_data, _REFUND_CONTAINERS)
                    refunded = _sum_refunds(refund_data, refund_records)
                    refund_state = _refund_state(refund_data, refund_records)

        refundable: Decimal | None
        if not refund_required:
            refundable = _ZERO
        elif captured is None or refunded is None:
            refundable = None
        else:
            refundable = max(captured - refunded, _ZERO)

        verdict = "insufficient_evidence"
        if duplicate:
            verdict = "duplicate_capture"
        elif refund_state is not None:
            verdict = refund_state
        elif captured is not None and expected is not None:
            verdict = "reconciled" if abs(captured - expected) <= _CENT else "capture_mismatch"

        return PaymentResult(
            verdict=verdict,
            captured_total_brl=_as_float(captured),
            refunded_total_brl=_as_float(refunded),
            refundable_total_brl=_as_float(refundable),
            payment_references=references,
            evidence_refs=list(dict.fromkeys(evidence_refs))[:20],
        )
