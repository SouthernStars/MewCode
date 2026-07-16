from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from mewcode.config import ConfigError, load_config
from mewcode.hooks import HookConfigError, HookEngine, load_hooks
from mewcode.permissions import PermissionMode


def main() -> None:
    # 先确保 .mewcode/ 目录存在，否则下面写 debug.log 会因目录不存在而崩溃
    Path(".mewcode").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        filename=".mewcode/debug.log",
        filemode="w",
    )

    parser = argparse.ArgumentParser(prog="mewcode", description="MewCode AI coding assistant")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=None,
        help="Permission mode (overrides config.yaml)",
    )
    parser.add_argument(
        "-p",
        metavar="PROMPT",
        default=None,
        help="Run non-interactively: execute the prompt and print the result to stdout",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    mode_str = args.mode if args.mode else config.permission_mode
    permission_mode = PermissionMode(mode_str)

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as e:
        print(f"Hook config error: {e}", file=sys.stderr)
        sys.exit(1)

    hook_engine = HookEngine(hooks) if hooks else None

    if args.p is not None:
        asyncio.run(_run_prompt(config, permission_mode, hook_engine, args.p))
        return

    from mewcode.app import MewCodeApp
    from mewcode.driver import NoAltScreenDriver

    app = MewCodeApp(
        config=config,
        permission_mode=permission_mode,
        hook_engine=hook_engine,
        driver_class=NoAltScreenDriver,
    )
    app.run()


async def _run_prompt(config, permission_mode, hook_engine, prompt: str) -> None:
    from mewcode.runtime import (
        RuntimeBuilder,
        RuntimeCapabilities,
        RuntimeSettings,
    )

    provider = config.providers[0]
    settings = RuntimeSettings.from_app_config(
        config,
        provider=provider,
        permission_mode=permission_mode,
    )
    runtime = RuntimeBuilder(
        settings,
        work_dir=os.getcwd(),
        capabilities=RuntimeCapabilities.noninteractive(),
        hook_engine=hook_engine,
    ).build()
    await runtime.refresh_context_window()
    await runtime.start()

    agent = runtime.agent
    task_manager = runtime.task_manager
    team_manager = runtime.team_manager
    conv = runtime.conversation

    def drain_notifications() -> list[str]:
        notes: list[str] = []
        for t in task_manager.poll_completed():
            notes.append(
                f"<task-notification>\n<task_id>{t.id}</task_id>\n"
                f"<status>{t.status}</status>\n<result>{t.result}</result>\n"
                f"</task-notification>"
            )
        notes.extend(team_manager.drain_lead_mailbox())
        return notes

    try:
        last_result = await agent.run_to_completion(prompt, conv)
        print(last_result, flush=True)

        for _ in range(90):
            if not team_manager._teams:
                break
            await asyncio.sleep(2)
            running = {
                task_id: not task.done()
                for task_id, task in task_manager._async_tasks.items()
            }
            notes = drain_notifications()
            if not notes:
                if not any(running.values()):
                    break
                continue
            for note in notes:
                conv.add_system_reminder(note)
            last_result = await agent.run_to_completion(
                "Teammate notifications received. Process them and continue.", conv
            )
            print(last_result, flush=True)
    finally:
        await runtime.shutdown()


if __name__ == "__main__":
    main()

