from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

_EVIDENCE_REF = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")


def normalise_field(value: Any) -> str:
    """Return a stable comparison key for API fields and human labels."""

    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def first_value(record: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    normalised = {normalise_field(key): value for key, value in record.items()}
    for key in keys:
        value = normalised.get(normalise_field(key))
        if value is not None:
            return value
    return None


def walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_mappings(child)


def evidence_data(evidence: Any) -> Any:
    if isinstance(evidence, Mapping) and "data" in evidence:
        return evidence["data"]
    return evidence


def evidence_ref(evidence: Any) -> str | None:
    if not isinstance(evidence, Mapping):
        return None
    value = evidence.get("evidence_ref")
    if isinstance(value, str) and _EVIDENCE_REF.fullmatch(value):
        return value
    return None


def unique_strings(values: Any, limit: int = 20) -> list[str]:
    if isinstance(values, (str, int, float)) and not isinstance(values, bool):
        values = [values]
    if not isinstance(values, list | tuple | set):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            continue
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def collect_identifiers(
    value: Any,
    singular_keys: tuple[str, ...],
    plural_keys: tuple[str, ...] = (),
    limit: int = 20,
) -> list[str]:
    singular = {normalise_field(key) for key in singular_keys}
    plural = {normalise_field(key) for key in plural_keys}
    values: list[str] = []
    for mapping in walk_mappings(value):
        for raw_key, raw_value in mapping.items():
            key = normalise_field(raw_key)
            if key in singular:
                values.extend(unique_strings([raw_value], limit))
            elif key in plural:
                values.extend(unique_strings(raw_value, limit))
            if len(values) >= limit:
                return list(dict.fromkeys(values))[:limit]
    return list(dict.fromkeys(values))[:limit]


def collect_texts(value: Any, keys: tuple[str, ...], limit: int = 40) -> list[str]:
    wanted = {normalise_field(key) for key in keys}
    result: list[str] = []
    for mapping in walk_mappings(value):
        for raw_key, raw_value in mapping.items():
            if normalise_field(raw_key) not in wanted:
                continue
            if isinstance(raw_value, (str, int, float)) and not isinstance(raw_value, bool):
                text = str(raw_value).strip().lower().replace("-", "_").replace(" ", "_")
                if text and text not in result:
                    result.append(text)
            if len(result) >= limit:
                return result
    return result


def collect_bools(value: Any, keys: tuple[str, ...]) -> list[bool]:
    wanted = {normalise_field(key) for key in keys}
    result: list[bool] = []
    for mapping in walk_mappings(value):
        for raw_key, raw_value in mapping.items():
            if normalise_field(raw_key) not in wanted:
                continue
            if isinstance(raw_value, bool):
                result.append(raw_value)
            elif isinstance(raw_value, str):
                text = raw_value.strip().lower()
                if text in {"true", "yes", "1"}:
                    result.append(True)
                elif text in {"false", "no", "0"}:
                    result.append(False)
    return result


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def first_datetime(value: Any, keys: tuple[str, ...]) -> datetime | None:
    wanted = {normalise_field(key) for key in keys}
    for mapping in walk_mappings(value):
        for raw_key, raw_value in mapping.items():
            if normalise_field(raw_key) in wanted:
                parsed = parse_datetime(raw_value)
                if parsed is not None:
                    return parsed
    return None


def has_key(value: Any, keys: tuple[str, ...]) -> bool:
    wanted = {normalise_field(key) for key in keys}
    return any(
        normalise_field(key) in wanted
        for mapping in walk_mappings(value)
        for key in mapping
    )


def is_negative_result(value: Any) -> bool:
    statuses = set(
        collect_texts(
            value,
            ("status", "result", "state", "order_status", "resolution_status"),
        )
    )
    return bool(statuses & {"not_found", "notfound", "missing", "unknown"})
