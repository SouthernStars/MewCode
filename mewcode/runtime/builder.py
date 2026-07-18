from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from mewcode.agent import Agent
from mewcode.agents.loader import AgentLoader
from mewcode.agents.metrics import MetricsCollector
from mewcode.agents.task_manager import TaskManager
from mewcode.agents.trace import TraceManager
from mewcode.cache import FileCache
from mewcode.client import LLMClient, create_client, resolve_context_window
from mewcode.execution_context import ExecutionContext
from mewcode.config import ProviderConfig
from mewcode.context.critic import CompletenessCritic
from mewcode.conversation import ConversationManager
from mewcode.filehistory import FileHistory
from mewcode.harness.config_manager import ConfigManager
from mewcode.harness.hook_manager import HookManager
from mewcode.harness.permission_manager import PermissionManager
from mewcode.harness.tools import (
    AddHookTool,
    AddPermissionRuleTool,
    ListHooksTool,
    ManageMemoryTool,
    RemoveHookTool,
    RemovePermissionRuleTool,
    UpdateConfigTool,
)
from mewcode.hooks import HookContext, HookEngine
from mewcode.mcp import MCPManager
from mewcode.observability import (
    EventMetricsAggregator,
    JsonlEventSink,
    RuntimeEventBus,
)
from mewcode.memory import MemoryManager, Session, SessionManager, load_instructions
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.permissions.audit import AuditLogger
from mewcode.permissions.rate_limit import RateLimiter
from mewcode.scheduler.runtime import SchedulerRuntime
from mewcode.scheduler.store import CronJob, CronStore
from mewcode.scheduler.tools import (
    CronCreateTool,
    CronDeleteTool,
    CronListTool,
    ScheduleWakeupTool,
)
from mewcode.scheduler.wakeup import WakeupScheduler
from mewcode.skills.executor import SkillExecutor
from mewcode.skills.loader import SkillLoader
from mewcode.teams.manager import TeamManager
from mewcode.task_supervisor import TaskSupervisor
from mewcode.tools import ToolRegistry, create_default_registry
from mewcode.tools.agent_tool import AgentTool
from mewcode.tools.ask_user import AskUserTool
from mewcode.tools.enter_worktree import EnterWorktreeTool
from mewcode.tools.exit_plan_mode import ExitPlanModeTool
from mewcode.tools.exit_worktree import ExitWorktreeTool
from mewcode.tools.impl.tool_search import ToolSearchTool
from mewcode.tools.load_skill import LoadSkill
from mewcode.tools.synthetic_output import SyntheticOutputTool
from mewcode.tools.team_create import TeamCreateTool
from mewcode.tools.team_delete import TeamDeleteTool
from mewcode.workflow.engine import WorkflowEngine
from mewcode.workflow.tool import ListWorkflowsTool, WorkflowTool
from mewcode.worktree.cleanup import start_stale_cleanup_task
from mewcode.worktree.manager import WorktreeManager

from .config import RuntimeCapabilities, RuntimeConfigState, RuntimeSettings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeCallbacks:
    on_workflow_log: Callable[[str], None] = field(default=lambda _message: None)
    on_scheduled_fire: Callable[[CronJob], None] = field(default=lambda _job: None)
    on_mcp_ready: Callable[[list[str], str], None] = field(
        default=lambda _errors, _instructions: None
    )


@dataclass
class Runtime:
    settings: RuntimeSettings
    capabilities: RuntimeCapabilities
    callbacks: RuntimeCallbacks
    task_supervisor: TaskSupervisor
    work_dir: str
    provider: ProviderConfig
    client: LLMClient
    conversation: ConversationManager
    registry: ToolRegistry
    agent: Agent
    file_cache: FileCache
    memory_manager: MemoryManager
    session_manager: SessionManager
    session: Session
    file_history: FileHistory
    task_manager: TaskManager
    trace_manager: TraceManager
    event_bus: RuntimeEventBus
    event_metrics: EventMetricsAggregator
    skill_loader: SkillLoader
    skill_executor: SkillExecutor
    load_skill_tool: LoadSkill
    worktree_manager: WorktreeManager
    agent_loader: AgentLoader
    team_manager: TeamManager
    workflow_engine: WorkflowEngine
    cron_store: CronStore
    wakeup_scheduler: WakeupScheduler
    scheduler_runtime: SchedulerRuntime
    critic: CompletenessCritic
    audit_logger: AuditLogger
    rate_limiter: RateLimiter
    metrics_collector: MetricsCollector
    hook_manager: HookManager
    config_state: RuntimeConfigState
    config_manager: ConfigManager
    permission_manager: PermissionManager
    mcp_manager: MCPManager
    exit_plan_tool: ExitPlanModeTool | None = None
    evolution_manager: Any = None
    mcp_instructions: str = ""
    mcp_errors: list[str] = field(default_factory=list)
    _stale_cleanup_task: asyncio.Task[None] | None = None
    _started: bool = False

    async def refresh_context_window(self) -> None:
        await resolve_context_window(self.provider)
        self.agent.context_window = self.provider.get_context_window()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True

        if self.agent.hook_engine:
            await self.agent.hook_engine.run_hooks(
                "startup", HookContext(event_name="startup")
            )

        if self.capabilities.scheduler:
            await self.scheduler_runtime.start()

        self._stale_cleanup_task = self.task_supervisor.create(
            start_stale_cleanup_task(
                self.worktree_manager,
                self.settings.worktree_stale_cleanup_interval,
                self.settings.worktree_stale_cutoff_hours,
            ),
            name="worktree.stale_cleanup",
        )

        if self.capabilities.mcp and self.settings.mcp_servers:
            await self.start_mcp()

    async def start_mcp(self) -> None:
        tools_before = len(self.registry.list_tools())
        self.mcp_errors = await self.mcp_manager.register_all_tools(self.registry)
        tools_after = len(self.registry.list_tools())
        if self.mcp_manager._clients and tools_after > tools_before:
            sections: list[str] = []
            for config in self.settings.mcp_servers:
                tool_names = [
                    tool.name
                    for tool in self.registry.list_tools()
                    if tool.name.startswith(f"mcp__{config.name}__")
                ]
                section = f"## {config.name}\n"
                if tool_names:
                    section += "Available tools: " + ", ".join(tool_names)
                sections.append(section)
            self.mcp_instructions = (
                "# MCP Server Instructions\n\n"
                "The following MCP servers are connected. Use their tools when the user asks.\n\n"
                + "\n\n".join(sections)
            )
        self.callbacks.on_mcp_ready(self.mcp_errors, self.mcp_instructions)

    async def shutdown(self) -> None:
        self.task_supervisor.begin_shutdown()

        if self._stale_cleanup_task and not self._stale_cleanup_task.done():
            self.task_supervisor.cancel(self._stale_cleanup_task)
            try:
                await self._stale_cleanup_task
            except asyncio.CancelledError:
                pass
            self._stale_cleanup_task = None

        await self.scheduler_runtime.shutdown()
        await self.mcp_manager.shutdown()
        await self.task_supervisor.cancel_all()

        if self.agent.memory_manager:
            try:
                await self.agent._extract_memories(self.conversation)
            except Exception:
                log.error("Runtime memory extraction failed during shutdown", exc_info=True)

        if self.evolution_manager is not None:
            try:
                self.evolution_manager.trace_collector.flush()
                await self.evolution_manager.check_and_evolve()
                self.evolution_manager.cleanup()
            except Exception:
                log.error("Evolution cleanup failed during shutdown", exc_info=True)

        for name in list(self.team_manager._teams):
            try:
                team = self.team_manager._teams[name]
                for member in team.members:
                    team.set_member_active(member.name, False)
                self.team_manager.delete_team(name)
            except Exception:
                log.error("Team cleanup failed for %s", name, exc_info=True)

        if self.agent.hook_engine:
            await self.agent.hook_engine.run_hooks(
                "shutdown",
                HookContext(event_name="shutdown"),
                wait_for_async=True,
            )

        await self.task_supervisor.shutdown()
        self.event_metrics.save(
            Path(self.work_dir)
            / ".mewcode"
            / "metrics"
            / f"{self.session.session_id}.events.json"
        )
        self.session.close()
        self._started = False


class RuntimeBuilder:
    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        work_dir: str,
        capabilities: RuntimeCapabilities,
        hook_engine: HookEngine | None = None,
        callbacks: RuntimeCallbacks | None = None,
    ) -> None:
        self.settings = settings
        self.work_dir = str(Path(work_dir).resolve())
        self.capabilities = capabilities
        self.hook_engine = hook_engine
        self.callbacks = callbacks or RuntimeCallbacks()

    def build(self) -> Runtime:
        work_path = Path(self.work_dir)
        work_path.mkdir(parents=True, exist_ok=True)
        home = Path.home()

        file_cache = FileCache()
        memory_manager = MemoryManager(self.work_dir)
        session_manager = SessionManager(self.work_dir)
        session_manager.cleanup()
        session = session_manager.create()
        file_history = FileHistory(self.work_dir, session.session_id)
        conversation = ConversationManager()
        path_sandbox = PathSandbox(self.work_dir)
        registry = create_default_registry(
            file_cache=file_cache,
            file_history=file_history,
            project_root=self.work_dir,
            path_sandbox=path_sandbox,
        )
        provider = self.settings.provider.to_config()
        client = create_client(provider)
        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=path_sandbox,
            rule_engine=RuleEngine(
                user_rules_path=home / ".mewcode" / "permissions.yaml",
                project_rules_path=work_path / ".mewcode" / "permissions.yaml",
                local_rules_path=work_path / ".mewcode" / "permissions.local.yaml",
            ),
            mode=self.settings.permission_mode,
        )
        instructions = load_instructions(self.work_dir)
        task_supervisor = TaskSupervisor(session_id=session.session_id)
        if self.hook_engine is not None:
            self.hook_engine.bind_task_supervisor(task_supervisor)

        agent = Agent(
            client=client,
            registry=registry,
            protocol=provider.protocol,
            work_dir=self.work_dir,
            permission_checker=checker,
            context_window=provider.get_context_window(),
            instructions_content=instructions,
            memory_manager=memory_manager,
            hook_engine=self.hook_engine,
            task_supervisor=task_supervisor,
        )
        agent.file_history = file_history
        agent.session_id = session.session_id
        task_supervisor.agent_id = agent.agent_id

        load_skill_tool = LoadSkill()
        registry.register(load_skill_tool)
        registry.register(
            ToolSearchTool(registry, protocol=provider.protocol)
        )

        exit_plan_tool: ExitPlanModeTool | None = None
        if self.capabilities.permission_prompts:
            registry.register(AskUserTool())

        if self.capabilities.interactive_ui:
            exit_plan_tool = ExitPlanModeTool(
                is_plan_mode=lambda: agent.plan_mode,
                plan_exists=lambda: agent._get_plan_path().exists(),
            )
            registry.register(exit_plan_tool)

        skill_loader = SkillLoader(self.work_dir)
        skill_loader.load_all()
        load_skill_tool.set_loader(skill_loader)
        load_skill_tool.set_agent(agent)
        skill_executor = SkillExecutor(
            agent=agent,
            client=client,
            protocol=provider.protocol,
        )
        self._configure_skill_catalog(agent, skill_loader)

        worktree_manager = WorktreeManager(
            repo_root=self.work_dir,
            file_cache=file_cache,
            symlink_directories=list(self.settings.worktree_symlink_directories),
        )
        restored = worktree_manager.restore_session()
        if restored:
            agent.work_dir = restored.worktree_path
        registry.register(EnterWorktreeTool(worktree_manager=worktree_manager))
        registry.register(ExitWorktreeTool(worktree_manager=worktree_manager))

        task_manager = TaskManager(task_supervisor=task_supervisor)
        event_bus = RuntimeEventBus()
        event_metrics = EventMetricsAggregator(session_id=session.session_id)
        event_bus.subscribe(event_metrics)
        event_bus.subscribe(
            JsonlEventSink(
                str(Path(self.work_dir) / ".mewcode" / "events" / f"{session.session_id}.jsonl")
            )
        )
        agent.event_bus = event_bus
        trace_manager = TraceManager()
        execution_context = ExecutionContext(
            work_dir=self.work_dir,
            session_id=session.session_id,
            agent_id=agent.agent_id,
            task_supervisor=task_supervisor,
            event_bus=event_bus,
            trace_manager=trace_manager,
            permission_checker=checker,
        )
        agent.execution_context = execution_context
        agent_loader = AgentLoader(
            self.work_dir,
            enable_verification=self.settings.enable_verification_agent,
        )
        agent_loader.load_all()
        team_manager = TeamManager(
            worktree_manager=worktree_manager,
            trace_manager=trace_manager,
            execution_context=execution_context,
        )
        registry.register(
            AgentTool(
                agent_loader=agent_loader,
                task_manager=task_manager,
                trace_manager=trace_manager,
                parent_agent=agent,
                enable_fork=self.settings.enable_fork,
                provider_config=provider,
                worktree_manager=worktree_manager,
                team_manager=team_manager,
            )
        )
        teammate_mode = (
            self.settings.teammate_mode
            if self.capabilities.interactive_ui
            else "in-process"
        )
        registry.register(
            TeamCreateTool(
                team_manager=team_manager,
                parent_agent=agent,
                teammate_mode=teammate_mode,
                is_interactive=self.capabilities.interactive_ui,
                enable_coordinator_mode=self.settings.enable_coordinator_mode,
            )
        )
        registry.register(TeamDeleteTool(team_manager=team_manager, parent_agent=agent))
        registry.register(SyntheticOutputTool())
        agent._team_manager = team_manager
        agent.notification_fn = team_manager.drain_lead_mailbox
        self._configure_agent_catalog(agent, agent_loader)

        workflow_engine = WorkflowEngine(
            work_dir=self.work_dir,
            on_log=self.callbacks.on_workflow_log,
            execution_context=execution_context,
        )
        registry.register(
            WorkflowTool(
                engine=workflow_engine,
                task_manager=task_manager,
                task_supervisor=task_supervisor,
            )
        )
        registry.register(ListWorkflowsTool(engine=workflow_engine))
        agent.workflow_engine = workflow_engine

        cron_store = CronStore(self.work_dir)
        wakeup_scheduler = WakeupScheduler()
        scheduler_runtime: SchedulerRuntime

        def on_scheduled_fire(job: CronJob) -> None:
            conversation.add_system_reminder(scheduler_runtime.inject_job(job))
            self.callbacks.on_scheduled_fire(job)

        scheduler_runtime = SchedulerRuntime(
            cron_store=cron_store,
            wakeup_scheduler=wakeup_scheduler,
            on_fire=on_scheduled_fire,
            task_supervisor=task_supervisor,
        )
        registry.register(CronCreateTool(cron_store))
        registry.register(CronDeleteTool(cron_store))
        registry.register(CronListTool(cron_store))
        registry.register(ScheduleWakeupTool(wakeup_scheduler))

        critic = CompletenessCritic(enabled=self.settings.critic_enabled)
        audit_logger = AuditLogger(self.work_dir, session_id=session.session_id)
        rate_limiter = RateLimiter(
            enabled=self.settings.rate_limit_enabled,
            default_max_per_minute=self.settings.rate_limit_default_max_per_minute,
            per_tool_limits=dict(self.settings.rate_limit_per_tool),
        )
        metrics_collector = MetricsCollector(
            self.work_dir,
            session_id=session.session_id,
        )
        agent.critic = critic
        agent.audit_logger = audit_logger
        agent.rate_limiter = rate_limiter
        agent.metrics_collector = metrics_collector

        hook_manager = HookManager(hook_engine=self.hook_engine)
        config_state = self.settings.make_mutable_state()
        config_state.max_iterations = agent.max_iterations
        config_manager = ConfigManager(
            config_state,
            on_change={
                "permission_mode": lambda value: agent.set_permission_mode(
                    PermissionMode(value)
                ),
                "critic.enabled": lambda value: setattr(critic, "enabled", bool(value)),
                "rate_limit.enabled": lambda value: setattr(
                    rate_limiter, "enabled", bool(value)
                ),
                "rate_limit.default_max_per_minute": lambda value: setattr(
                    rate_limiter, "default_max", int(value)
                ),
                "max_iterations": lambda value: setattr(
                    agent, "max_iterations", int(value)
                ),
            },
        )
        permission_manager = PermissionManager(
            work_dir=self.work_dir,
            rule_engine=checker.rule_engine,
        )
        if self.settings.allow_self_modification:
            for tool in (
                AddHookTool(hook_manager),
                RemoveHookTool(hook_manager),
                ListHooksTool(hook_manager),
                UpdateConfigTool(config_manager),
                AddPermissionRuleTool(permission_manager),
                RemovePermissionRuleTool(permission_manager),
                ManageMemoryTool(memory_manager),
            ):
                registry.register(tool)

        evolution_manager = self._build_evolution_manager(
            client=client,
            session_manager=session_manager,
            memory_manager=memory_manager,
            skill_loader=skill_loader,
            registry=registry,
        )

        mcp_manager = MCPManager()
        mcp_manager.load_configs(
            [server.to_config() for server in self.settings.mcp_servers]
        )

        return Runtime(
            settings=self.settings,
            capabilities=self.capabilities,
            callbacks=self.callbacks,
            task_supervisor=task_supervisor,
            work_dir=self.work_dir,
            provider=provider,
            client=client,
            conversation=conversation,
            registry=registry,
            agent=agent,
            file_cache=file_cache,
            memory_manager=memory_manager,
            session_manager=session_manager,
            session=session,
            file_history=file_history,
            task_manager=task_manager,
            trace_manager=trace_manager,
            event_bus=event_bus,
            event_metrics=event_metrics,
            skill_loader=skill_loader,
            skill_executor=skill_executor,
            load_skill_tool=load_skill_tool,
            worktree_manager=worktree_manager,
            agent_loader=agent_loader,
            team_manager=team_manager,
            workflow_engine=workflow_engine,
            cron_store=cron_store,
            wakeup_scheduler=wakeup_scheduler,
            scheduler_runtime=scheduler_runtime,
            critic=critic,
            audit_logger=audit_logger,
            rate_limiter=rate_limiter,
            metrics_collector=metrics_collector,
            hook_manager=hook_manager,
            config_state=config_state,
            config_manager=config_manager,
            permission_manager=permission_manager,
            mcp_manager=mcp_manager,
            exit_plan_tool=exit_plan_tool,
            evolution_manager=evolution_manager,
        )

    def _configure_skill_catalog(self, agent: Agent, loader: SkillLoader) -> None:
        catalog = loader.get_catalog()
        if not catalog:
            return
        lines = ["You can use the following Skills:", ""]
        lines.extend(f"- {name}: {description}" for name, description in catalog)
        lines.extend(
            [
                "",
                "If the user's request matches a Skill, call LoadSkill to activate it.",
            ]
        )
        agent.set_skill_catalog("\n".join(lines))

    def _configure_agent_catalog(self, agent: Agent, loader: AgentLoader) -> None:
        catalog = loader.list_agents()
        if not catalog:
            return
        lines = [
            "## Available Sub-Agent Types",
            "",
            "Use the Agent tool with subagent_type parameter to delegate tasks:",
            "",
        ]
        lines.extend(f"- **{agent_type}**: {when}" for agent_type, when in catalog)
        if self.settings.enable_fork:
            lines.extend(
                [
                    "",
                    "Leave subagent_type empty to fork the current conversation "
                    "(inherits full dialog history).",
                ]
            )
        lines.extend(
            [
                "",
                "IMPORTANT: Sub-agents run in the background. "
                "After calling the Agent tool, you will get a task ID immediately. "
                "Do NOT wait, sleep, or poll for the result. "
                "Simply report the task ID to the user and end your turn. "
                "The system will automatically notify when the task completes.",
            ]
        )
        agent.set_agent_catalog("\n".join(lines), catalog_list=catalog)

    def _build_evolution_manager(
        self,
        *,
        client: LLMClient,
        session_manager: SessionManager,
        memory_manager: MemoryManager,
        skill_loader: SkillLoader,
        registry: ToolRegistry,
    ) -> Any:
        if not self.settings.allow_self_evolution:
            return None

        from mewcode.harness.evolution.manager import EvolutionManager
        from mewcode.harness.evolution.tools import (
            DeprecateSkillTool,
            GetEvolutionDetailTool,
            ListAutoSkillsTool,
            ListEvolutionsTool,
            TriggerEvolutionTool,
        )
        from mewcode.tools.base import StreamEnd, TextDelta

        async def evolution_client(prompt: str, system: str = "", model_hint: str = "haiku") -> str:
            del model_hint
            conversation = ConversationManager()
            conversation.add_user_message(prompt)
            collected = ""
            async for event in client.stream(conversation, system=system):
                if isinstance(event, TextDelta):
                    collected += event.text
                elif isinstance(event, StreamEnd):
                    continue
            return collected

        manager = EvolutionManager(
            harness_dir=Path(self.work_dir) / "mewcode" / "harness",
            config=self.settings.evolution.to_config(),
            client_factory=evolution_client,
            session_manager=session_manager,
            memory_manager=memory_manager,
            skill_loader=skill_loader,
        )
        for tool in (
            TriggerEvolutionTool(manager),
            ListEvolutionsTool(manager),
            GetEvolutionDetailTool(manager),
            ListAutoSkillsTool(manager.skill_meta_manager),
            DeprecateSkillTool(manager.skill_meta_manager),
        ):
            registry.register(tool)
        return manager
