from __future__ import annotations

import json
from pathlib import Path

import pytest

from mewcode.agents.metrics import MetricsCollector


def test_metrics_snapshot_is_versioned_and_round_trips_raw_aggregates(
    tmp_path: Path,
) -> None:
    collector = MetricsCollector(str(tmp_path), "session-1")
    collector.record_tool_call(12.5)
    collector.record_tool_call(30.0)
    collector.record_agent_call(10, 4, cache_hit=True)
    collector.record_agent_call(8, 2, cache_hit=False)
    collector.record_compact()
    collector.finalize()

    path = tmp_path / ".mewcode" / "metrics" / "session-1.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == 1
    assert snapshot["tool_latencies"] == [12.5, 30.0]
    assert snapshot["prompt_cache_hits"] == 1
    assert snapshot["prompt_cache_misses"] == 1
    assert snapshot["total_requests"] == 2

    loaded = MetricsCollector.load(str(tmp_path), "session-1")
    assert loaded is not None
    assert loaded.tool_latencies == [12.5, 30.0]
    assert loaded.cache_hit_rate == 0.5
    assert loaded.total_tokens == 24
    assert loaded.compact_count == 1


def test_metrics_load_migrates_v0_and_rejects_corruption(tmp_path: Path) -> None:
    path = tmp_path / ".mewcode" / "metrics" / "legacy.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "session_id": "legacy",
                "total_tokens": 7,
                "total_tool_calls": 2,
            }
        ),
        encoding="utf-8",
    )
    loaded = MetricsCollector.load(str(tmp_path), "legacy")
    assert loaded is not None
    assert loaded.total_tokens == 7
    assert loaded.total_tool_calls == 2

    path.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="metrics snapshot"):
        MetricsCollector.load(str(tmp_path), "legacy")
