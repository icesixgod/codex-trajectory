# Codex Trajectory plugin

**English** · [简体中文](#简体中文)

**Version 1.0.0** · October 2, 2026 · [Changelog](../../CHANGELOG.md#100---2026-10-02)

View local Codex task history as an interactive timeline and event ledger, with timing, tokens, estimated costs, and live activity.

**Calculate task timing** reads only the currently open task's complete history and reuses its loaded summary index. View durations and percentages for local/tool execution, estimated model/token time, and unattributed time. Gaps between turns are excluded and overlapping tools count once. Model timing includes response/network waits and does not isolate pure token generation.

Coverage distinguishes missing, invalid, zero, and sub-millisecond tool durations. Zero and below-precision values do not block model estimates; missing/invalid tools reserve only their possible execution windows as unattributed. Public event durations keep their existing display format.

Choose **+ → More tools… → Plugins and MCPs → Codex Trajectory** in the task's right panel, or open the trajectory through the `show_codex_trajectory` MCP tool. Both pages include **Current task timing distribution** below the top statistics. The native task-panel entry opens without a model turn. Valid task context opens that exact task's safe summary; unavailable context shows a local task selector. Full details require confirmation, and live views always use safe summaries. Source logs remain unchanged.

After updating an existing installation, fully quit and reopen Codex to reload the plugin registration.

See the [English README](../../README.md) for installation, screenshots, usage, and privacy. Technical details are in the [interface reference](../../docs/interface.md) and [native viewer guide](../../docs/native-viewer.md).

## 简体中文

**版本 1.0.0** · 2026 年 10 月 2 日 · [更新日志](../../CHANGELOG.md#100---2026-10-02)

将本地 Codex 任务历史展示为交互时间轴和事件账本，查看耗时、Token、估算费用和实时活动。

点击**统计当前任务耗时**，仅读取本任务的完整历史并复用已加载的摘要索引。展示本地／工具执行、模型／Token 估算和未归因时间的时长与占比；排除轮次间空档，并行工具的重叠时间只计算一次。模型时间包含响应及网络等待，不能代表纯 Token 生成耗时。

计时覆盖区分缺失、无效、零值和低于 1 毫秒的工具耗时。零值及低于精度的耗时不阻塞模型估算；缺失／无效工具仅将可能执行区间保留为未归因。事件耗时沿用原有显示格式。

在任务右侧面板中选择 **+ → More tools… → Plugins and MCPs → Codex Trajectory**，或通过 `show_codex_trajectory` MCP 工具打开轨迹。两种页面均在顶部统计卡片下方提供**当前任务耗时分布**。原生任务面板入口不触发模型对话。有有效任务上下文时，精确展示该任务的安全摘要；上下文不可用时，显示本地任务选择器。完整详情需要确认，实时视图始终使用安全摘要，源日志保持不变。

更新已有安装后，请完整退出并重新打开 Codex，以重新加载插件注册。

安装、截图、使用和隐私说明见[中文 README](../../README.zh-CN.md)。技术细节见[接口文档](../../docs/interface.md)和[原生查看器指南](../../docs/native-viewer.md)。
