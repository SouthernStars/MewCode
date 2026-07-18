from __future__ import annotations

from pathlib import Path

from mewcode.observability import (
    EventMetricsAggregator,
    EventType,
    JsonlEventSink,
    RuntimeEvent,
    RuntimeEventBus,
)
from mewcode.persistence import read_jsonl_records


def test_runtime_event_has_causal_context_and_round_trips() -> None:
    event = RuntimeEvent(
        event_type=EventType.TOOL_CALL,
        session_id="session-1",
        run_id="run-1",
        agent_id="agent-1",
        parent_agent_id="parent-1",
        tool_call_id="tool-1",
        timestamp=123.0,
        event_id="event-1",
        payload={"tool_name": "Read"},
    )

    assert RuntimeEvent.from_dict(event.to_dict()) == event


def test_event_bus_preserves_order_and_unsubscribes() -> None:
    events: list[RuntimeEvent] = []
    bus = RuntimeEventBus()
    unsubscribe = bus.subscribe(events.append)

    bus.emit(EventType.LLM_REQUEST, payload={"iteration": 1})
    unsubscribe()
    bus.emit(EventType.LLM_RESPONSE, payload={"iteration": 1})

    assert [event.event_type for event in events] == [EventType.LLM_REQUEST]


def test_event_bus_isolates_optional_sink_failures() -> None:
    seen: list[RuntimeEvent] = []
    bus = RuntimeEventBus()
    bus.subscribe(lambda _event: (_ for _ in ()).throw(RuntimeError("sink down")))
    bus.subscribe(seen.append)

    bus.emit(EventType.ERROR, payload={"message": "boom"})

    assert len(seen) == 1


def test_jsonl_event_sink_persists_events(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(str(path))
    event = RuntimeEvent(event_type=EventType.WORKFLOW_STAGE, payload={"stage": "plan"})

    sink(event)

    assert read_jsonl_records(path, format_name="runtime events") == [event.to_dict()]


def test_event_metrics_aggregate_core_quality_signals(tmp_path: Path) -> None:
    metrics = EventMetricsAggregator(session_id="session-1")
    metrics(RuntimeEvent(EventType.LLM_REQUEST, session_id="session-1"))
    metrics(
        RuntimeEvent(
            EventType.LLM_RESPONSE,
            session_id="session-1",
            payload={"input_tokens": 10, "output_tokens": 5},
        )
    )
    metrics(
        RuntimeEvent(
            EventType.TOOL_RESULT,
            session_id="session-1",
            payload={"is_error": False, "elapsed": 12.0},
        )
    )
    metrics(
        RuntimeEvent(
            EventType.PERMISSION_DECISION,
            session_id="session-1",
            payload={"decision": "deny"},
        )
    )

    snapshot_path = tmp_path / "metrics.json"
    metrics.save(snapshot_path)
    snapshot = metrics.snapshot()

    assert snapshot["event_count"] == 4
    assert snapshot["total_tokens"] == 15
    assert snapshot["tool_success_rate"] == 1.0
    assert snapshot["permission_denials"] == 1
    assert snapshot_path.exists()
