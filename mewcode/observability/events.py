"""A small, typed event spine shared by runtime consumers.

The bus is deliberately synchronous: event producers never need to create a
background task just to publish telemetry, and subscribers can decide whether
to persist, render, or aggregate the event.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mewcode.persistence import append_jsonl_record

log = logging.getLogger(__name__)


class EventType(StrEnum):
    LLM_REQUEST = "llm.request"
    LLM_RESPONSE = "llm.response"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    PERMISSION_DECISION = "permission.decision"
    AGENT_DELEGATION = "agent.delegation"
    WORKFLOW_STAGE = "workflow.stage"
    SCHEDULER_TRIGGER = "scheduler.trigger"
    ERROR = "runtime.error"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """One causally attributable runtime event."""

    event_type: EventType
    session_id: str = ""
    run_id: str = ""
    agent_id: str = ""
    parent_agent_id: str = ""
    tool_call_id: str = ""
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "parent_agent_id": self.parent_agent_id,
            "tool_call_id": self.tool_call_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeEvent:
        try:
            event_type = EventType(data["event_type"])
            return cls(
                event_id=str(data["event_id"]),
                event_type=event_type,
                timestamp=float(data["timestamp"]),
                session_id=str(data.get("session_id", "")),
                run_id=str(data.get("run_id", "")),
                agent_id=str(data.get("agent_id", "")),
                parent_agent_id=str(data.get("parent_agent_id", "")),
                tool_call_id=str(data.get("tool_call_id", "")),
                payload=dict(data.get("payload", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid runtime event: {exc}") from exc


class JsonlEventSink:
    """Persist events without making persistence part of the producer path."""

    def __init__(self, path: str) -> None:
        self._path = path

    def __call__(self, event: RuntimeEvent) -> None:
        append_jsonl_record(
            self._path,
            event.to_dict(),
            format_name="runtime events",
        )


Subscriber = Callable[[RuntimeEvent], None]


class RuntimeEventBus:
    """Publish runtime events to zero or more synchronous subscribers."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(subscriber)
            except ValueError:
                return

        return unsubscribe

    def emit(
        self,
        event_type: EventType,
        *,
        session_id: str = "",
        run_id: str = "",
        agent_id: str = "",
        parent_agent_id: str = "",
        tool_call_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        event = RuntimeEvent(
            event_type=event_type,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
            tool_call_id=tool_call_id,
            payload=dict(payload or {}),
        )
        for subscriber in tuple(self._subscribers):
            try:
                subscriber(event)
            except Exception:
                log.warning(
                    "Runtime event subscriber failed: event_id=%s type=%s",
                    event.event_id,
                    event.event_type,
                    exc_info=True,
                )
        return event
