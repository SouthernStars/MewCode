from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import shutil
import platform
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

from mewcode.config import ConfigError, load_config
from mewcode.hooks import HookConfigError, HookEngine, load_hooks
from mewcode.permissions import PermissionMode

PROJECT_CONFIG = """# MewCode project configuration
permission_mode: default
providers:
  - name: default
    protocol: anthropic
    base_url: https://api.anthropic.com
    model: claude-3-5-sonnet-latest
    api_key: ${ANTHROPIC_API_KEY}
"""


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
    parser.add_argument(
        "command",
        nargs="?",
        choices=("init", "doctor", "version"),
        help="Project maintenance command",
    )
    args = parser.parse_args()

    if args.command == "version":
        print(_version())
        return
    if args.command == "init":
        _init_project(Path.cwd())
        return
    if args.command == "doctor":
        raise SystemExit(_doctor(Path.cwd()))

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


def _version() -> str:
    try:
        return package_version("mewcode")
    except PackageNotFoundError:
        return "0.2.0"


def _init_project(root: Path) -> None:
    config_dir = root / ".mewcode"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    if config_path.exists():
        print(f"配置已存在：{config_path}")
    else:
        config_path.write_text(PROJECT_CONFIG, encoding="utf-8")
        print(f"已创建：{config_path}")
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".mewcode/debug.log\n.mewcode/sessions/\n", encoding="utf-8")
        print(f"已创建：{gitignore}")


def _doctor(root: Path) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python", sys.version_info >= (3, 11), platform.python_version()))
    checks.append(("Git", shutil.which("git") is not None, "可用"))
    config_path = root / ".mewcode" / "config.yaml"
    checks.append(("配置文件", config_path.exists(), str(config_path)))
    try:
        config = load_config(config_path)
    except (ConfigError, OSError) as exc:
        checks.append(("配置校验", False, str(exc)))
    else:
        checks.append(("Provider", bool(config.providers), f"{len(config.providers)} 个"))
        for provider in config.providers:
            key = provider.resolve_api_key()
            key_ok = bool(key) and not key.startswith("${")
            checks.append((f"API Key/{provider.name}", key_ok, "已设置" if key_ok else "未设置"))

    failed = 0
    for name, ok, detail in checks:
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")
        failed += not ok
    return 1 if failed else 0


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

