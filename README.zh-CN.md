# Codex Trajectory

[![CI](https://github.com/icesixgod/codex-trajectory/actions/workflows/ci.yml/badge.svg)](https://github.com/icesixgod/codex-trajectory/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/icesixgod/codex-trajectory)](https://github.com/icesixgod/codex-trajectory/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[English](README.md)

Codex Trajectory 是一个只读 Codex 插件，可将本地任务日志转换为兼顾隐私的事件账本和交互式时间轴。它展示轮次、近似模型步骤、推理摘要、助手消息、工具耗时、子代理、上下文压缩、Token 用量和失败信息，不会修改原始日志。

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

完整详情必须通过 `detailLevel: "full"` 或界面确认按钮明确开启。完整详情会进入当前 Codex 对话，但基础指令和加密推理仍不会返回。插件的 Python 运行时没有遥测，也不会主动发起应用网络请求；`uv` 启动器可能按用户自己的 uv 配置下载兼容的 Python。详情见 [PRIVACY.md](PRIVACY.md)。

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

会话日志采用增量解析，内存中只保留请求的记录页，以及有上限的轮次、警告和工具调用状态；聚合统计仍覆盖完整的已解析任务。JSON 对象必须无歧义且可互操作：重复键、非有限数字、过大的整数和超过 16 MiB 的完整行会被拒绝或报告。重复的累计 Token 快照会被去重，部分有效快照会保留此前仍有效的计数，未变化的会话概览则按整条历史链上所有文件的元数据缓存。插件只会发现配置会话根目录下的普通单链接文件，并拒绝符号链接、硬链接和类似路径的会话选择器。界面刷新从有界的 500 条记录尾页开始，不再请求 1000 条上限；更早页面只在用户点击时加载。插件不会持续轮询，因为反复扫描仍在增长的超大日志反而会增加负载。

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
