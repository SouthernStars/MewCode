from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mewcode.client import LLMClient
from mewcode.config import (
    AppConfig,
    CriticConfig,
    MCPServerConfig,
    ProviderConfig,
    RateLimitConfig,
)
from mewcode.permissions import PermissionMode
from mewcode.hooks import Action, ActionResult, Hook, HookEngine
from mewcode.scheduler.store import CronJob
from mewcode.runtime import (
    RuntimeBuilder,
    RuntimeCapabilities,
    RuntimeSettings,
)


class FakeClient(LLMClient):
    async def stream(self, conversation, system="", tools=None):
        if False:
            yield None


def make_config(*, allow_self_modification: bool = False) -> AppConfig:
    return AppConfig(
        providers=[
            ProviderConfig(
                name="test",
                protocol="openai",
                base_url="https://api.example.com/v1",
                model="test-model",
                api_key="test-key",
            )
        ],
        mcp_servers=[
            MCPServerConfig(name="docs", command="python", args=["server.py"])
        ],
        critic=CriticConfig(enabled=False),
        rate_limit=RateLimitConfig(
            enabled=True,
            default_max_per_minute=7,
            per_tool={"Bash": 2},
        ),
        allow_self_modification=allow_self_modification,
    )


def test_runtime_settings_are_an_immutable_snapshot() -> None:
    config = make_config()

    settings = RuntimeSettings.from_app_config(
        config,
        provider=config.providers[0],
        permission_mode=PermissionMode.BYPASS,
    )

    config.critic.enabled = True
    config.rate_limit.per_tool["Bash"] = 99
    config.mcp_servers[0].args.append("--changed")

    assert settings.permission_mode is PermissionMode.BYPASS
    assert settings.critic_enabled is False
    assert settings.rate_limit_per_tool == (("Bash", 2),)
    assert settings.mcp_servers[0].args == ("server.py",)


def test_builder_equips_interactive_and_noninteractive_consistently(
    tmp_path: Path,
) -> None:
    config = make_config()
    interactive_settings = RuntimeSettings.from_app_config(
        config,
        provider=config.providers[0],
        permission_mode=PermissionMode.DEFAULT,
    )
    noninteractive_settings = RuntimeSettings.from_app_config(
        config,
        provider=config.providers[0],
        permission_mode=PermissionMode.DONT_ASK,
    )

    with patch("mewcode.runtime.builder.create_client", return_value=FakeClient()):
        interactive = RuntimeBuilder(
            interactive_settings,
            work_dir=str(tmp_path / "interactive"),
            capabilities=RuntimeCapabilities.interactive(),
        ).build()
        noninteractive = RuntimeBuilder(
            noninteractive_settings,
            work_dir=str(tmp_path / "noninteractive"),
            capabilities=RuntimeCapabilities.noninteractive(),
        ).build()

    expected_common = {
        "ReadFile",
        "WriteFile",
        "EditFile",
        "Bash",
        "Glob",
        "Grep",
        "ToolSearch",
        "LoadSkill",
        "Agent",
        "TeamCreate",
        "TeamDelete",
        "Workflow",
        "ListWorkflows",
        "CronCreate",
        "CronDelete",
        "CronList",
        "ScheduleWakeup",
    }
    interactive_tools = {tool.name for tool in interactive.registry.list_tools()}
    noninteractive_tools = {tool.name for tool in noninteractive.registry.list_tools()}

    assert expected_common <= interactive_tools
    assert expected_common <= noninteractive_tools
    assert {"AskUserQuestion", "ExitPlanMode"} <= interactive_tools
    assert "AskUserQuestion" not in noninteractive_tools
    assert "ExitPlanMode" not in noninteractive_tools
    assert interactive.agent.registry is interactive.registry
    assert noninteractive.agent.registry is noninteractive.registry
    assert interactive.agent.task_supervisor is interactive.task_supervisor
    assert interactive.task_manager.task_supervisor is interactive.task_supervisor
    assert interactive.workflow_engine is not None
    assert noninteractive.workflow_engine is not None
    assert interactive.scheduler_runtime is not None
    assert noninteractive.scheduler_runtime is not None


def test_runtime_config_service_does_not_mutate_app_config(tmp_path: Path) -> None:
    config = make_config(allow_self_modification=True)
    settings = RuntimeSettings.from_app_config(
        config,
        provider=config.providers[0],
        permission_mode=PermissionMode.DEFAULT,
    )

    with patch("mewcode.runtime.builder.create_client", return_value=FakeClient()):
        runtime = RuntimeBuilder(
            settings,
            work_dir=str(tmp_path),
            capabilities=RuntimeCapabilities.noninteractive(),
        ).build()

    success, _ = runtime.config_manager.set_config("critic.enabled", True)
    assert success is True
    assert runtime.critic.enabled is True
    assert config.critic.enabled is False

    success, _ = runtime.config_manager.set_config("permission_mode", "dontAsk")
    assert success is True
    assert runtime.agent.permission_mode is PermissionMode.DONT_ASK
    assert config.permission_mode == "default"


@pytest.mark.asyncio
async def test_tui_adopts_builder_runtime(tmp_path: Path, monkeypatch) -> None:
    from mewcode.app import MewCodeApp

    monkeypatch.chdir(tmp_path)
    config = make_config()
    config.mcp_servers = []

    with patch("mewcode.runtime.builder.create_client", return_value=FakeClient()):
        app = MewCodeApp(config=config)
        async with app.run_test():
            assert app.runtime is not None
            assert app.agent is app.runtime.agent
            assert app.registry is app.runtime.registry
            assert app.team_manager is app.runtime.team_manager
            assert app.workflow_engine is app.runtime.workflow_engine
            assert app.scheduler_runtime is app.runtime.scheduler_runtime
            assert "ui.notification_poll" in (
                app.runtime.task_supervisor.active_names
            )
            await app.runtime.shutdown()


@pytest.mark.asyncio
async def test_runtime_lifecycle_and_scheduler_injection(tmp_path: Path) -> None:
    config = make_config()
    config.mcp_servers = []
    settings = RuntimeSettings.from_app_config(
        config,
        provider=config.providers[0],
        permission_mode=PermissionMode.DONT_ASK,
    )
    fired: list[str] = []

    from mewcode.runtime import RuntimeCallbacks

    with patch("mewcode.runtime.builder.create_client", return_value=FakeClient()):
        runtime = RuntimeBuilder(
            settings,
            work_dir=str(tmp_path),
            capabilities=RuntimeCapabilities.noninteractive(),
            callbacks=RuntimeCallbacks(
                on_scheduled_fire=lambda job: fired.append(job.id)
            ),
        ).build()

    await runtime.start()
    assert runtime.scheduler_runtime._running is True
    assert runtime._stale_cleanup_task is not None
    assert set(runtime.task_supervisor.active_names) == {
        "scheduler.loop",
        "worktree.stale_cleanup",
    }

    runtime.scheduler_runtime._on_fire(
        CronJob(id="job-1", cron="* * * * *", prompt="continue")
    )
    assert fired == ["job-1"]
    assert "Scheduled recurring task fired: continue" in (
        runtime.conversation.history[-1].content
    )

    await runtime.shutdown()
    assert runtime.scheduler_runtime._running is False
    assert runtime._stale_cleanup_task is None
    assert runtime.task_supervisor.active_names == ()
    assert runtime.session._file.closed is True


@pytest.mark.asyncio
async def test_runtime_shutdown_cancels_background_agents(tmp_path: Path) -> None:
    config = make_config()
    config.mcp_servers = []
    settings = RuntimeSettings.from_app_config(
        config,
        provider=config.providers[0],
        permission_mode=PermissionMode.DONT_ASK,
    )
    started = asyncio.Event()

    class SlowAgent:
        team_name = ""
        _team_manager = None
        total_input_tokens = 0
        total_output_tokens = 0

        async def run_to_completion(self, task, conversation=None):
            started.set()
            await asyncio.Event().wait()

    with patch("mewcode.runtime.builder.create_client", return_value=FakeClient()):
        runtime = RuntimeBuilder(
            settings,
            work_dir=str(tmp_path),
            capabilities=RuntimeCapabilities.noninteractive(),
        ).build()

    task_id = runtime.task_manager.launch(SlowAgent(), "background work")
    await asyncio.wait_for(started.wait(), timeout=1)

    await runtime.shutdown()

    assert runtime.task_manager.get(task_id).status == "cancelled"
    assert runtime.task_supervisor.active_names == ()


@pytest.mark.asyncio
async def test_runtime_shutdown_waits_for_async_shutdown_hooks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = make_config()
    config.mcp_servers = []
    settings = RuntimeSettings.from_app_config(
        config,
        provider=config.providers[0],
        permission_mode=PermissionMode.DONT_ASK,
    )
    completed = asyncio.Event()

    async def shutdown_action(action, context):
        completed.set()
        return ActionResult(output="done", success=True)

    monkeypatch.setattr(
        "mewcode.hooks.engine.execute_action",
        shutdown_action,
    )
    hook_engine = HookEngine(
        [
            Hook(
                id="flush",
                event="shutdown",
                action=Action(type="command", command="ignored"),
                async_exec=True,
            )
        ]
    )

    with patch("mewcode.runtime.builder.create_client", return_value=FakeClient()):
        runtime = RuntimeBuilder(
            settings,
            work_dir=str(tmp_path),
            capabilities=RuntimeCapabilities.noninteractive(),
            hook_engine=hook_engine,
        ).build()

    await runtime.shutdown()

    assert completed.is_set()
    assert runtime.task_supervisor.active_names == ()


@pytest.mark.asyncio
async def test_memory_prefetch_failure_is_logged(
    tmp_path: Path,
    caplog,
) -> None:
    from mewcode.app import MewCodeApp
    from mewcode.memory import MemoryManager

    config = make_config()
    app = MewCodeApp(config=config)
    app.memory_manager = MemoryManager(str(tmp_path))
    app._selected_provider = config.providers[0]

    with caplog.at_level(logging.ERROR), patch(
        "mewcode.app.find_relevant_memories",
        side_effect=RuntimeError("recall failed"),
    ):
        result = await app._prefetch_relevant_memories("where is auth")

    assert result == ""
    assert "where is auth" in caplog.text
    assert "test-model" in caplog.text
    assert "recall failed" in caplog.text


@pytest.mark.asyncio
async def test_session_summary_failure_is_logged(caplog) -> None:
    from mewcode.app import MewCodeApp

    config = make_config()
    app = MewCodeApp(config=config)
    app.session = SimpleNamespace(session_id="session-failed")
    app.client = FakeClient()
    app.agent = SimpleNamespace(protocol="openai")

    with caplog.at_level(logging.ERROR), patch(
        "mewcode.app.generate_session_summary",
        side_effect=RuntimeError("summary failed"),
    ):
        await app._update_session_summary()

    assert "session-failed" in caplog.text
    assert "summary failed" in caplog.text
