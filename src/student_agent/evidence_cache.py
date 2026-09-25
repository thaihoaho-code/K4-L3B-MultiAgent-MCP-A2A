"""Case-scoped MCP evidence store: cache, bounded retry, least privilege and provenance.

One store exists per case. Cache keys are ``(case_id, tool_name, normalized_arguments)`` so an
evidence object can never leak into another case. Deterministic tool errors (for example "no
refund rows for this order") are cached as misses and never retried; transient transport
errors get at most ``max_retries`` extra attempts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from .agent_types import TOOL_PERMISSIONS


class Gateway(Protocol):
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]: ...


class TraceSink(Protocol):
    def emit(self, **kwargs: Any) -> dict[str, Any]: ...


class ToolPermissionError(PermissionError):
    pass


class CrossCaseEvidenceError(ValueError):
    pass


class CallBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Evidence:
    ref: str
    tool: str
    domain: str
    data: Any
    warnings: tuple[str, ...] = ()


@dataclass
class CallRecord:
    actor: str
    tool: str
    ok: bool
    attempts: int


# Process-wide registry of server-issued refs -> owning case. Only used to *detect* reuse of an
# evidence ref across cases; it never serves evidence to another case.
_REF_OWNERS: dict[str, str] = {}


def _is_deterministic_tool_error(exc: Exception) -> bool:
    return isinstance(exc, RuntimeError) and str(exc).startswith("MCP tool ")


@dataclass
class CaseEvidenceStore:
    case_id: str
    gateway: Gateway
    trace: TraceSink
    max_retries: int = 1
    call_budget: int = 12
    call_timeout: float = 90.0
    _cache: dict[tuple[str, str, tuple[tuple[str, str], ...]], Evidence | None] = field(
        default_factory=dict
    )
    _locks: dict[tuple[str, str, tuple[tuple[str, str], ...]], asyncio.Lock] = field(
        default_factory=dict
    )
    _by_ref: dict[str, Evidence] = field(default_factory=dict)
    _consumed: dict[str, set[str]] = field(default_factory=dict)
    calls: list[CallRecord] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def fetch(self, actor: str, tool: str, **arguments: str) -> Evidence | None:
        """Return evidence for ``tool`` or ``None`` when the server has no such record."""
        if tool not in TOOL_PERMISSIONS.get(actor, frozenset()):
            raise ToolPermissionError(f"{actor} is not allowed to call {tool}")
        normalized = tuple(sorted((key, str(value).strip()) for key, value in arguments.items()))
        key = (self.case_id, tool, normalized)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._cache:
                return self._cache[key]
            if self.call_count >= self.call_budget:
                raise CallBudgetExceeded(f"{self.case_id}: MCP call budget exhausted")
            evidence = await self._call_with_retry(actor, tool, dict(normalized))
            self._cache[key] = evidence
            return evidence

    async def _call_with_retry(
        self, actor: str, tool: str, arguments: dict[str, str]
    ) -> Evidence | None:
        attempts = 0
        while True:
            attempts += 1
            try:
                raw = await asyncio.wait_for(
                    self.gateway.call(tool, case_id=self.case_id, **arguments),
                    timeout=self.call_timeout,
                )
            except Exception as exc:  # noqa: BLE001 - classified below
                if _is_deterministic_tool_error(exc):
                    self.calls.append(CallRecord(actor, tool, False, attempts))
                    return None
                if isinstance(exc, ValueError) or attempts > self.max_retries:
                    self.calls.append(CallRecord(actor, tool, False, attempts))
                    raise
                continue
            self.calls.append(CallRecord(actor, tool, True, attempts))
            return self._register(tool, raw)

    def _register(self, tool: str, raw: dict[str, Any]) -> Evidence:
        ref = raw["evidence_ref"]
        owner = _REF_OWNERS.setdefault(ref, self.case_id)
        if owner != self.case_id:
            raise CrossCaseEvidenceError(f"{ref} already belongs to {owner}")
        evidence = Evidence(
            ref=ref,
            tool=tool,
            domain=raw["domain"],
            data=raw.get("data"),
            warnings=tuple(raw.get("warnings") or ()),
        )
        self._by_ref[ref] = evidence
        return evidence

    def consume(self, actor: str, evidence: Evidence | None) -> str | None:
        """Mark evidence as actually used by ``actor`` and emit ``tool_result_consumed`` once."""
        if evidence is None:
            return None
        if self._by_ref.get(evidence.ref) is not evidence:
            raise CrossCaseEvidenceError(f"{evidence.ref} was not issued for {self.case_id}")
        seen = self._consumed.setdefault(evidence.ref, set())
        if actor not in seen:
            seen.add(actor)
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=evidence.tool,
                evidence_refs=[evidence.ref],
            )
        return evidence.ref

    def owns(self, ref: str) -> bool:
        return ref in self._by_ref

    def consumed_refs(self) -> list[str]:
        return list(self._consumed)

    def get(self, ref: str) -> Evidence | None:
        return self._by_ref.get(ref)

    def by_tool(self, tool: str) -> list[Evidence]:
        return [item for item in self._by_ref.values() if item.tool == tool]
