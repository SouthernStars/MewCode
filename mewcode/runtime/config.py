from __future__ import annotations

from dataclasses import dataclass, field

from mewcode.config import (
    AppConfig,
    CompactConfig,
    CriticConfig,
    EvolutionConfig,
    MCPServerConfig,
    ProviderConfig,
    RateLimitConfig,
)
from mewcode.permissions import PermissionMode


@dataclass(frozen=True)
class RuntimeProviderSettings:
    name: str
    protocol: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    thinking: bool
    context_window: int
    max_output_tokens: int

    @classmethod
    def from_config(cls, config: ProviderConfig) -> RuntimeProviderSettings:
        return cls(
            name=config.name,
            protocol=config.protocol,
            base_url=config.base_url,
            model=config.model,
            api_key=config.api_key,
            thinking=config.thinking,
            context_window=config.context_window,
            max_output_tokens=config.max_output_tokens,
        )

    def to_config(self) -> ProviderConfig:
        return ProviderConfig(**self.__dict__)


@dataclass(frozen=True)
class RuntimeMCPServerSettings:
    name: str
    command: str | None
    args: tuple[str, ...]
    url: str | None
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    env: tuple[tuple[str, str], ...] = field(repr=False)

    @classmethod
    def from_config(cls, config: MCPServerConfig) -> RuntimeMCPServerSettings:
        return cls(
            name=config.name,
            command=config.command,
            args=tuple(config.args),
            url=config.url,
            headers=tuple(config.headers.items()),
            env=tuple(config.env.items()),
        )

    def to_config(self) -> MCPServerConfig:
        return MCPServerConfig(
            name=self.name,
            command=self.command,
            args=list(self.args),
            url=self.url,
            headers=dict(self.headers),
            env=dict(self.env),
        )


@dataclass(frozen=True)
class RuntimeEvolutionSettings:
    enabled: bool
    min_traces_trigger: int
    max_traces_per_evolution: int
    min_traces_per_evolution: int
    min_failure_recurrence: int
    token_increase_threshold: float
    deprecation_task_threshold: int
    backup_dir: str
    traces_dir: str
    skills_dir: str
    skill_meta_file: str

    @classmethod
    def from_config(cls, config: EvolutionConfig) -> RuntimeEvolutionSettings:
        return cls(
            enabled=config.enabled,
            min_traces_trigger=config.min_traces_trigger,
            max_traces_per_evolution=config.max_traces_per_evolution,
            min_traces_per_evolution=config.min_traces_per_evolution,
            min_failure_recurrence=config.min_failure_recurrence,
            token_increase_threshold=config.token_increase_threshold,
            deprecation_task_threshold=config.deprecation_task_threshold,
            backup_dir=config.backup_dir,
            traces_dir=config.traces_dir,
            skills_dir=config.skills_dir,
            skill_meta_file=config.skill_meta_file,
        )

    def to_config(self) -> EvolutionConfig:
        return EvolutionConfig(**self.__dict__)


@dataclass(frozen=True)
class RuntimeSettings:
    """Read-only snapshot used while assembling one runtime instance."""

    provider: RuntimeProviderSettings
    permission_mode: PermissionMode
    mcp_servers: tuple[RuntimeMCPServerSettings, ...]
    enable_fork: bool
    enable_verification_agent: bool
    worktree_symlink_directories: tuple[str, ...]
    worktree_stale_cleanup_interval: int
    worktree_stale_cutoff_hours: int
    teammate_mode: str
    enable_coordinator_mode: bool
    compact_utilization_threshold: float
    compact_min_keep_messages: int
    critic_enabled: bool
    rate_limit_enabled: bool
    rate_limit_default_max_per_minute: int
    rate_limit_per_tool: tuple[tuple[str, int], ...]
    allow_self_modification: bool
    allow_self_evolution: bool
    evolution: RuntimeEvolutionSettings

    @classmethod
    def from_app_config(
        cls,
        config: AppConfig,
        *,
        provider: ProviderConfig,
        permission_mode: PermissionMode,
    ) -> RuntimeSettings:
        provider_names = {candidate.name for candidate in config.providers}
        if provider.name not in provider_names:
            raise ValueError(
                f"Runtime provider '{provider.name}' is not present in AppConfig"
            )
        return cls(
            provider=RuntimeProviderSettings.from_config(provider),
            permission_mode=permission_mode,
            mcp_servers=tuple(
                RuntimeMCPServerSettings.from_config(server)
                for server in config.mcp_servers
            ),
            enable_fork=config.enable_fork,
            enable_verification_agent=config.enable_verification_agent,
            worktree_symlink_directories=tuple(config.worktree.symlink_directories),
            worktree_stale_cleanup_interval=config.worktree.stale_cleanup_interval,
            worktree_stale_cutoff_hours=config.worktree.stale_cutoff_hours,
            teammate_mode=config.teammate_mode,
            enable_coordinator_mode=config.enable_coordinator_mode,
            compact_utilization_threshold=config.compact.utilization_threshold,
            compact_min_keep_messages=config.compact.min_keep_messages,
            critic_enabled=config.critic.enabled,
            rate_limit_enabled=config.rate_limit.enabled,
            rate_limit_default_max_per_minute=config.rate_limit.default_max_per_minute,
            rate_limit_per_tool=tuple(config.rate_limit.per_tool.items()),
            allow_self_modification=config.allow_self_modification,
            allow_self_evolution=config.allow_self_evolution,
            evolution=RuntimeEvolutionSettings.from_config(config.evolution),
        )

    def make_mutable_state(self) -> RuntimeConfigState:
        return RuntimeConfigState(
            permission_mode=self.permission_mode.value,
            compact=CompactConfig(
                utilization_threshold=self.compact_utilization_threshold,
                min_keep_messages=self.compact_min_keep_messages,
            ),
            critic=CriticConfig(enabled=self.critic_enabled),
            rate_limit=RateLimitConfig(
                enabled=self.rate_limit_enabled,
                default_max_per_minute=self.rate_limit_default_max_per_minute,
                per_tool=dict(self.rate_limit_per_tool),
            ),
        )


@dataclass
class RuntimeConfigState:
    """Mutable runtime state owned by ConfigManager, separate from AppConfig."""

    permission_mode: str
    compact: CompactConfig
    critic: CriticConfig
    rate_limit: RateLimitConfig
    max_iterations: int = 50


@dataclass(frozen=True)
class RuntimeCapabilities:
    interactive_ui: bool
    permission_prompts: bool
    mcp: bool = True
    scheduler: bool = True

    def __post_init__(self) -> None:
        if self.permission_prompts and not self.interactive_ui:
            raise ValueError(
                "permission_prompts requires an interactive UI event consumer"
            )

    @classmethod
    def interactive(cls) -> RuntimeCapabilities:
        return cls(interactive_ui=True, permission_prompts=True)

    @classmethod
    def noninteractive(cls) -> RuntimeCapabilities:
        return cls(interactive_ui=False, permission_prompts=False)
