# Codex Trajectory

[![CI](https://github.com/icesixgod/codex-trajectory/actions/workflows/ci.yml/badge.svg)](https://github.com/icesixgod/codex-trajectory/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/icesixgod/codex-trajectory)](https://github.com/icesixgod/codex-trajectory/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[English](README.md)

Codex Trajectory 是一个兼顾隐私的 Codex 插件，其 MCP 工具可将本地任务日志转换为事件账本和交互式时间轴。它展示轮次、近似模型步骤、推理摘要、助手消息、工具耗时、子代理、上下文压缩、Token 用量和失败信息，不会修改原始日志。可选的实时停止控件只使用用户明确开启的本机回环 CDP：先暂停活跃 Goal，再直接中断当前回合；不会发送后续消息、进入“调整方向”队列、删除 worktree 或修改任务文件。

![Codex Trajectory 移动端界面](plugins/codex-trajectory/assets/screenshots/mobile-zh.png)

## 安装

前置条件：macOS、Linux 或 Windows 上已安装 Codex 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```sh
codex plugin marketplace add icesixgod/codex-trajectory
codex plugin add codex-trajectory@icesixgod
```

打开一个新的 Codex 任务，使插件工具和 Skill 生效，然后输入：

> 显示这个 Codex 任务的安全轨迹摘要。

## 隐私设计

默认使用安全摘要模式：返回事件名称、时间、状态、Token 用量和有长度限制的摘要，同时隐藏工具输入/输出、原始记录元数据、绝对日志路径、Git 远程地址、基础指令和加密推理。

完整详情必须通过 `detailLevel: "full"` 或界面确认按钮明确开启。完整详情会进入当前 Codex 对话，但基础指令和加密推理仍不会返回。插件的 Python 运行时没有遥测；只有用户明确开启可选 CDP 直停与内置浏览器入口时，它才会使用本机回环端口。`uv` 启动器可能按用户自己的 uv 配置下载兼容的 Python。标准 Apps 与内置浏览器界面的停止都直接暂停 Goal 并中断回合，不调用模型、不发送任务消息，也不进入“调整方向”队列。详情见 [PRIVACY.md](PRIVACY.md)。

## 工具

| 工具 | 用途 |
| --- | --- |
| `list_codex_sessions` | 列出最近任务的元数据，不返回对话正文。 |
| `get_codex_trajectory` | 返回适合分析的结构化轨迹。 |
| `show_codex_trajectory` | 返回轨迹并渲染交互式 MCP Apps 界面。 |

后两个工具接受 `sessionId`、`maxRecords`（50–1000）、排他的 `beforeRecord` 游标、`includeArchived` 和 `detailLevel`（`summary` 或 `full`）。省略 `sessionId` 时读取最近任务，省略 `beforeRecord` 时读取最新尾页；将 `pagination.nextBeforeRecord` 传给下一次调用即可读取紧邻的前一页。

输出遵循 [`schemaVersion: 1`](schemas/trajectory-v1.schema.json)，同时支持旧式 rollout 和当前由 `history_base` 连接的分页历史。分页会话在拼接继承历史前会校验会话身份、字节边界和连续序号；子代理在 `subagent_history_start_ordinal` 之前复制自父任务的上下文不会计入子任务轨迹。Codex 日志不直接记录 DeepSeek Harness 的步骤边界，因此插件在工具结果后模型再次输出时开启一个近似步骤。没有账本含义的未知控制事件会被忽略；完整但损坏的 JSONL 或 UTF-8 行会进入 `warnings`，活跃写入产生的未完成 JSON 或 UTF-8 尾行则被容忍。接口说明见 [docs/interface.md](docs/interface.md)。

界面的 Token 详情面板会拆分输入、缓存读取、非缓存输入、输出和推理输出，并展示缓存命中率；按轮次汇总默认折叠。数百亿、数千亿等大 Token 总量会通过自适应字号完整显示，不再以省略号截断。缓存和推理计数分别是输入与输出的子集，不会重复计入总量。

事件账本默认只展开已载入的最后一轮。如果仍有前一页，“加载更早记录”会在顶部追加紧邻的 500 条记录，按稳定记录索引去重并保持当前账本视口不跳动；可重复点击直到载入完整任务。每个轮次摘要会分别显示本轮模型、非缓存输入、缓存读取和输出，整条摘要（包括三个 Token 汇总单元格）都可点击展开或折叠；展开后的列标题归属于该轮次，只在该轮记录可见期间吸顶，因此折叠轮次不会再隔开标题与其对应记录。由于轮次已经由标题明确，记录行只显示近似“步骤”，不再重复“轮次/步骤”。Event 与 Content 使用紧凑的比例列宽，三列 Token 无需水平滚动即可始终可见；鼠标悬停任一被截断字段时会显示完整内容，键盘聚焦记录时则同时展示两项。更早轮次和 Token 详情的按轮次表格仅在打开时才创建对应页面节点，从而降低大型任务的 DOM 开销；搜索、类型筛选、时间轴选择或直接选择记录时，会自动显示折叠轮次中的匹配记录。

点击“实时小窗”后，可以在不启动独立 App 的情况下持续查看当前任务。在 Codex 内，组件会请求宿主支持的 `fullscreen` 展示模式并铺满右侧栏：顶部冻结显示整个任务的累计 Token 拆分及当前轮次/步骤/记录索引，剩余高度全部交给可独立滚动的安全摘要事件流。当 Codex 日志记录了账户限额窗口时，右上角的额度徽章会显示各窗口（通常为 5 小时与每周）的剩余百分比，悬浮可查看重置时间；没有这类数据时徽章自动隐藏。“停止”按钮和可选的“自动停止”都要求先开启实验性本机回环 CDP。标准 Apps 界面通过私有 `request_codex_task_stop` 工具，内置浏览器通过会话绑定接口，执行同一套 `thread/goal/set` 暂停活跃 Goal 加 `turn/interrupt` 直停序列；两者都不会发送后续消息、调用模型、等待“发送后续提示”确认框或进入“调整方向”队列。自动停止默认关闭，默认阈值为剩余 10%，同一额度周期最多触发一次；过期回合会重新绑定一次，暂时失败按有界退避重试。当前回合结束后，“已停止”会变为 Idle，之后的新回合重新启用手动停止；自动停止的防重复锁会跨 `/goal` 后续回合保留，直到额度恢复、窗口重置或用户修改设置。未连接直停 CDP，或页面切换到并非最初打开它的任务时，停止控件会禁用并显示明确原因。

每条事件显示状态、耗时，并将本条 Token 增量整理为“总计 / 输入 / 输出”三行：输入拆分非缓存输入与缓存读取，输出拆分可见输出与推理输出。只有单点时间戳、没有测得耗时区间的记录显示 `—`，不再显示误导性的 `0 ms`；真实测得的零耗时仍显示 `0 ms`。最新记录始终排在底部并自动跟随。紧凑的透明 32 帧挖矿鲸鱼娘完整收在最新记录卡片内部左上角，仅当最新记录身份变化时完整播放一轮；无变化轮询保持静止，系统启用“减少动态效果”时也不会播放。组件不会请求当前宿主不支持的 `pip` 模式。在没有 Codex 显示桥接的普通 Chromium 页面中，组件优先使用浏览器原生视频画中画，并在右上角绘制同一份额度摘要；如果画中画 API 不存在或在调用时被拒绝，则自动在当前页面内切换到同一套全高实时面板。不可交互的视频画中画不会显示停止控件；可交互的页面内回退在任一受支持停止桥接存在时会启用停止控件。完整工具输入、输出和原始元数据不会进入任何实时界面。它每秒串行检查一次，隐藏或退出后暂停，失败时自动退避；右侧栏外壳只创建一次并原位更新，未变化的轮询不会重建或移动状态栏。应用内专用刷新接口会先比较整条历史链的匿名修订号：任务未变化时不重新解析，变化时只返回最新 50 条安全摘要。常规入口仍要求用户亲自点击一次。

### 可选的无人值守直停与 Codex 内置浏览器入口

内联轨迹页面提供默认关闭的“无人值守直停与内置浏览器入口”开关。开启后，标准 Apps 的私有直停工具与无第三方依赖的本地监视进程只连接所选的 `127.0.0.1` Chrome DevTools Protocol 端口。私有工具接受有界的任务/回合标识、manual/auto 来源、阈值与语言，不接受任意提示词；当轨迹中的回合候选缺失或过期时，它从 App Server 读取一次当前活跃回合再执行固定的 Goal 暂停与中断序列。监视进程还会在随机回环端口提供带随机令牌的完整轨迹界面，并在 Codex React 重绘后持续把“查看轨迹”放回 `Full access` 右侧。内置浏览器与 MCP Apps 资源复用安全摘要、任务选择、统计、Token 详情、时间概览、筛选、账本、刷新、分页、完整详情确认和停止控件。打开或停止都不会填写输入框、发送任务消息、调用模型、新增轮次或触碰草稿与附件。内置浏览器的停止接口继续绑定打开它的会话与活跃回合；轻量 App Server 状态读取无需反复加载完整回合历史。无法暂停已确认的活跃 Goal 时不会中断回合，并显示有界错误。关闭开关会移除注入节点、关闭本地页面服务并停止监视进程；新版 MCP 运行时只会在验证旧 watcher 身份后替换它。设置、心跳和 watcher 锁仍仅作为 `CODEX_HOME/codex-trajectory` 下有大小限制的单链接普通文件读取，不会修改任务日志或保存主题值。

CDP 必须在应用启动时开启。请先完全退出 ChatGPT/Codex。macOS 可从终端重新启动：

```sh
open -a ChatGPT --args --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222
```

Windows 的 Microsoft Store 版本可在 PowerShell 中运行（未来版本的包名或可执行文件子路径可能变化）：

```powershell
$codex = Get-AppxPackage -Name OpenAI.Codex
$exe = Join-Path $codex.InstallLocation 'app\ChatGPT.exe'
Start-Process -FilePath $exe -ArgumentList '--remote-debugging-address=127.0.0.1','--remote-debugging-port=9222'
```

所选端口不可用时，轨迹页面也会显示这条命令。调试端口可以控制应用页面，因此必须只绑定回环地址，不要通过隧道或非本机地址暴露，不使用时应关闭页面中的开关。这是适配当前 Codex DOM 的实验性方案，并非官方插件工具栏接口。

会话日志采用增量解析，内存中只保留请求的记录页，以及有上限的轮次、警告和工具调用状态；聚合统计仍覆盖完整的已解析任务。JSON 对象必须无歧义且可互操作：重复键、非有限数字、过大的整数和超过 16 MiB 的完整行会被拒绝或报告。重复的累计 Token 快照会被去重，部分有效快照会保留此前仍有效的计数，未变化的会话概览则按整条历史链上所有文件的元数据缓存。插件只会发现配置会话根目录下的普通单链接文件，并拒绝符号链接、硬链接和类似路径的会话选择器。完整界面刷新从有界的 500 条记录尾页开始，不再请求 1000 条上限；更早页面只在用户点击时加载。

## 开发

```sh
uv sync --group dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest --cov --cov-report=term-missing
```

运行时没有第三方 Python 依赖，开发依赖由 `uv.lock` 锁定。贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 来源说明

事件账本、时间轴、选择和检查器实现的部分内容改编自 MIT 许可的 [`@deepseek-ai/dsh-client-ui-trajectory`](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/client/ui-trajectory)，上游版权声明为“Copyright (c) 2026 DeepSeek”。完整上游许可证收录于 [`LICENSES/DeepSeek-Harness.txt`](LICENSES/DeepSeek-Harness.txt)，其他信息见 [`NOTICE`](NOTICE)。

Codex Trajectory 是独立项目，与 DeepSeek 不存在隶属或背书关系。Codex 使用不同的持久化事件词汇；本仓库不捆绑 DeepSeek Harness 软件包、Cordis 运行时、React 运行时、TanStack Virtual 或 `diff` 软件包。

**友情链接**

[Linux.do](https://linux.do/)
