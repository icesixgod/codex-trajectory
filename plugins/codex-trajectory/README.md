# Codex Trajectory plugin

**English** · [简体中文](#简体中文)

View local Codex tasks as a searchable timeline and event ledger. Inspect tool calls and failures, calculate task timing, track tokens and estimated costs including descendant subagents, and follow live activity.

Open **+ → More tools… → Plugins and MCPs → Codex Trajectory** in the task's right panel, or use `show_codex_trajectory`. The panel opens the current task without a model turn; if task context is unavailable, choose a local task.

Safe summaries hide tool inputs and outputs. Full details require confirmation; live views always use safe summaries. Source logs remain unchanged. Timing and costs are estimates.

After updating, fully quit and reopen Codex.

See the [English README](../../README.md) for installation, screenshots, and usage, or the [interface reference](../../docs/interface.md) for MCP tools.

## 简体中文

将本地 Codex 任务展示为可搜索的时间轴和事件账本。查看工具调用与失败、统计任务耗时、汇总包含子代理的 Token 和估算费用，并跟随实时活动。

在任务右侧面板选择 **+ → More tools… → Plugins and MCPs → Codex Trajectory**，或使用 `show_codex_trajectory`。面板直接打开当前任务，无需触发模型对话；无法获取任务上下文时，手动选择本地任务。

默认安全摘要隐藏工具输入与输出，完整详情需要确认，实时视图始终使用安全摘要。源日志保持不变，耗时和费用均为估算。

更新后，请完整退出并重新打开 Codex。

安装、截图和使用方法见[中文 README](../../README.zh-CN.md)，MCP 工具见[接口文档](../../docs/interface.md)。
