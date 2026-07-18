from __future__ import annotations

from pathlib import Path
from typing import Any

from mewcode.conversation import ConversationManager, Message, ToolResultBlock, ToolUseBlock
from mewcode.persistence import (
    PersistenceError,
    atomic_write_json,
    file_lock,
    load_versioned_json,
)

TRANSCRIPT_SCHEMA_VERSION = 1


def _serialize_conversation(conv: ConversationManager) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for msg in conv.history:
        entry: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.tool_uses:
            entry["tool_uses"] = [
                {
                    "tool_use_id": tu.tool_use_id,
                    "tool_name": tu.tool_name,
                    "arguments": tu.arguments,
                }
                for tu in msg.tool_uses
            ]
        if msg.tool_results:
            entry["tool_results"] = [
                {
                    "tool_use_id": tr.tool_use_id,
                    "content": tr.content,
                    "is_error": tr.is_error,
                }
                for tr in msg.tool_results
            ]
        messages.append(entry)
    return messages


def _deserialize_conversation(data: list[dict[str, Any]]) -> ConversationManager:
    conv = ConversationManager()
    for entry in data:
        tool_uses = [
            ToolUseBlock(
                tool_use_id=tu["tool_use_id"],
                tool_name=tu["tool_name"],
                arguments=tu["arguments"],
            )
            for tu in entry.get("tool_uses", [])
        ]
        tool_results = [
            ToolResultBlock(
                tool_use_id=tr["tool_use_id"],
                content=tr["content"],
                is_error=tr.get("is_error", False),
            )
            for tr in entry.get("tool_results", [])
        ]
        msg = Message(
            role=entry["role"],
            content=entry.get("content", ""),
            tool_uses=tool_uses,
            tool_results=tool_results,
        )
        conv.history.append(msg)
    conv.env_injected = True
    conv.ltm_injected = True
    return conv


def save_transcript(
    team_name: str,
    agent_id: str,
    conversation: ConversationManager,
) -> Path:
    from mewcode.teams.models import resolve_team_dir

    transcript_dir = resolve_team_dir(team_name) / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"{agent_id}.json"
    data = {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "messages": _serialize_conversation(conversation),
    }
    with file_lock(path, format_name="team transcript snapshot"):
        atomic_write_json(path, data, format_name="team transcript snapshot")
    return path


def load_transcript(
    team_name: str,
    agent_id: str,
) -> ConversationManager | None:
    from mewcode.teams.models import resolve_team_dir

    path = resolve_team_dir(team_name) / "transcripts" / f"{agent_id}.json"
    if not path.exists():
        return None
    data = load_versioned_json(
        path,
        current_version=TRANSCRIPT_SCHEMA_VERSION,
        migrations={0: _migrate_transcript_v0},
        format_name="team transcript snapshot",
    )
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise PersistenceError(
            f"Invalid team transcript snapshot at {path}: "
            "messages must be a JSON array"
        )
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise PersistenceError(
                f"Invalid team transcript snapshot at {path}: "
                f"messages[{index}] must be a JSON object"
            )
    try:
        return _deserialize_conversation(messages)
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError(
            f"Invalid team transcript snapshot at {path}: {exc}"
        ) from exc


def _migrate_transcript_v0(data: Any) -> dict[str, Any]:
    if not isinstance(data, list):
        raise TypeError("legacy team transcript root must be a JSON array")
    return {"schema_version": 1, "messages": data}
