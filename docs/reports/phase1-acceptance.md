# Phase 1 验收报告

> 验收日期：2026-07-16
>
> 验收基线：`9f4b2d47481f9c9c384cce21358bf7113534379d`
>
> 结论：通过

## 1. 实施范围

Phase 1 按三个可独立回滚的 PR 完成：

1. [PR #2](https://github.com/SouthernStars/MewCode/pull/2)：引入只读
   `RuntimeSettings` 和统一 `RuntimeBuilder`。
2. [PR #3](https://github.com/SouthernStars/MewCode/pull/3)：删除
   `run_to_completion()` 内的第二套 Agent 循环，改为统一事件流的收集适配器。
3. [PR #4](https://github.com/SouthernStars/MewCode/pull/4)：统一串行与并发工具的
   Hook、限流、权限、参数校验、执行、审计和指标管线。

## 2. 验收结果

| 计划验收项 | 结果 | 证据 |
|---|---|---|
| TUI 与 `-p` 共用 Runtime 装配 | 通过 | 两个入口均调用 `RuntimeBuilder`；交互差异由 `RuntimeCapabilities` 显式表达 |
| 工具、Hook、权限和上下文行为一致 | 通过 | `test_runtime_builder.py`、`test_agent.py`、`test_permissions.py`、`test_hooks.py` 固定共同路径 |
| `run_to_completion()` 不含独立循环 | 通过 | 该方法只消费 `Agent.run()` 事件并收集最终文本 |
| `MewCodeApp` 不直接构造 Workflow、Scheduler、Harness、Team | 通过 | 相关构造仅存在于 `mewcode/runtime/builder.py` |
| 串行与并发工具经过同一安全管线 | 通过 | 生产代码仅保留一处 `prepared.tool.execute(params)`；并发写工具同样先审批 |
| 获批的并发安全工具仍保持并发 | 通过 | 并发屏障测试确认两个工具执行同时在途 |
| 原有测试无需大规模改写 | 通过 | 全量测试通过，新增测试集中在 Runtime 与 Agent 契约 |

## 3. 验证记录

### 静态检查

- 未发现 `_compact_config`、`_critic_config`、`_rate_limit_config`、
  `_allow_self_modification`、`_allow_self_evolution` 等构造后私有注入。
- `mewcode/app.py` 和 `mewcode/__main__.py` 未直接实例化
  `WorkflowEngine`、`SchedulerRuntime`、`TeamManager`、`ConfigManager`、
  `MCPManager` 或 `EvolutionManager`。
- 生产代码中只有一处底层工具执行调用：
  `mewcode/agent.py` 的统一已批准执行阶段。

### 自动化验证

```text
ruff check mewcode tests
All checks passed!

pytest -q tests/test_runtime_builder.py tests/test_agent.py \
  tests/test_permissions.py tests/test_hooks.py tests/test_harness.py
134 passed, 1 skipped

pytest -q -rs
582 passed, 1 skipped
```

唯一跳过项是 Windows 环境没有合适系统文件可用于符号链接目标的条件测试：
`tests/test_permissions.py:133`。该测试没有因 Phase 1 被新增跳过。

## 4. 边界与后续

- `MewCodeApp` 仍保留 Textual 渲染、事件消费、用户审批和 UI 状态适配，
  不再负责业务子系统构造。
- 后台任务仍由多个模块分别持有；集中监督、关闭顺序和异常上报属于 Phase 2。
- Session、Cron、Workflow Journal、Audit 和 Metrics 的版本化与原子写契约属于
  Phase 2，不在本阶段提前实现。

因此 Phase 1 可以关闭，下一步进入 Phase 2 的 `TaskSupervisor` 与生命周期加固。
