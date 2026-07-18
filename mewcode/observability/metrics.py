"""Deterministic aggregates derived from the RuntimeEvent stream."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mewcode.observability.events import EventType, RuntimeEvent
from mewcode.persistence import atomic_write_json, file_lock


@dataclass
class EventMetricsAggregator:
    """Consume events and expose stable runtime quality indicators."""

    session_id: str = ""
    event_count: int = 0
    llm_requests: int = 0
    llm_responses: int = 0
    tool_calls: int = 0
    tool_results: int = 0
    tool_errors: int = 0
    permission_denials: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    _tool_latencies: list[float] = field(default_factory=list, repr=False)

    def __call__(self, event: RuntimeEvent) -> None:
        self.event_count += 1
        if not self.session_id:
            self.session_id = event.session_id
        payload = event.payload
        if event.event_type == EventType.LLM_REQUEST:
            self.llm_requests += 1
        elif event.event_type == EventType.LLM_RESPONSE:
            self.llm_responses += 1
            self.input_tokens += _non_negative_int(payload.get("input_tokens"))
            self.output_tokens += _non_negative_int(payload.get("output_tokens"))
        elif event.event_type == EventType.TOOL_CALL:
            self.tool_calls += 1
        elif event.event_type == EventType.TOOL_RESULT:
            self.tool_results += 1
            if payload.get("is_error"):
                self.tool_errors += 1
            latency = payload.get("elapsed")
            if isinstance(latency, (int, float)) and latency >= 0:
                self._tool_latencies.append(float(latency))
        elif event.event_type == EventType.PERMISSION_DECISION:
            if payload.get("decision") == "deny":
                self.permission_denials += 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def tool_success_rate(self) -> float:
        if self.tool_results == 0:
            return 0.0
        return (self.tool_results - self.tool_errors) / self.tool_results

    def snapshot(self) -> dict[str, Any]:
        latencies = self._tool_latencies
        return {
            "schema_version": 1,
            "session_id": self.session_id,
            "event_count": self.event_count,
            "llm_requests": self.llm_requests,
            "llm_responses": self.llm_responses,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "tool_errors": self.tool_errors,
            "tool_success_rate": round(self.tool_success_rate, 4),
            "permission_denials": self.permission_denials,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "tool_latency_p50_ms": round(_percentile(latencies, 50), 2),
            "tool_latency_p95_ms": round(_percentile(latencies, 95), 2),
        }

    def save(self, path: str | Path) -> None:
        target = Path(path)
        with file_lock(target, format_name="event metrics"):
            atomic_write_json(target, self.snapshot(), format_name="event metrics")


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(
        values,
        n=100,
        method="inclusive",
    )[percentile - 1]
