# Native viewer migration

The viewer uses Codex's native global and conversation-panel extensions. Open **…** in Codex's left toolbar and choose **Codex Trajectory** for a fullscreen viewer; you can pin it to the toolbar. The existing right-panel **+ → More tools… → Plugins and MCPs → Codex Trajectory** entry remains available. Both entries use the same app-only tool and open without a model turn. Valid task context opens that exact task's safe summary. A missing or unavailable task context shows a local task selector, so the viewer never guesses another task from recent activity. After updating an existing installation, fully quit and reopen Codex to load the new entries. Toolbar pinning is controlled by Codex, and the plugin manifest cannot request automatic pinning.

The public session-list, trajectory-read, and interactive-show tools retain their defaults. Live Token/cost/quota views, compressed and paginated history, bounded full-detail confirmation, and the frozen v1/v2 schemas are preserved. Task status reflects the selected task; live account quota uses the newest valid sample found in recent local active-task logs, updates independently of task revisions, and shows its sample time in the tooltip. Both can lag the service. Manual stopping and quota-triggered automatic stopping have been removed.

After **Load full details**, leave **Default to full details for all tasks on this device** checked and continue once to remember the choice across tasks, reopened panels, and Codex restarts. Uncheck it for the selected task in this panel only. **Restore safe summary default** clears the opt-in and reloads a summary. The choice is stored locally as a boolean through app-only tools, so it works in sandboxed panels without browser storage. Public tool defaults, initial native results, and every live surface remain safe summaries; the saved choice enables on-demand detail reads without a subsequent full read on opening. Opening, task switching, refresh, and history pagination request summaries. Clicking a record requests at most 50 full records ending there; confirmation requests the current selection. Pending overlapping page reads are shared, errors offer an inspector retry, and a 5 MiB panel cache reuses loaded details until refresh, task change, revocation, or teardown. Stale responses are discarded. Uncached backend projections still scan the complete log for aggregates; summary reads skip formatting hidden detail fields.

## Structure

- `tools.py` owns MCP descriptors, argument dispatch, and native opening. The native tool embeds the packaged plugin icon as a PNG data URI for the toolbar and panel entries.
- `native_view.py` validates per-call context and defines selection responses.
- `protocol.py` transports request metadata without shared task identity.
- `projection.py` projects bounded local rollouts and loads the self-contained UI resource.
- `filesystem.py` shares link/reparse checks with log reads and the derived search index.
- `viewer_preferences.py` stores only the explicit full-detail default and rejects invalid or linked settings.
- `mcp-app-bridge.js` handles native initialization, tools, host context, display modes, and teardown. Fullscreen conversation panels return from the live view to the viewer without requesting an unsupported inline host mode. The legacy `window.openai` host remains compatible.

The runtime contains no debugging-port client, DOM injector, watcher, loopback HTTP viewer, or task-control RPC. The portable Windows no-console launcher remains because MCP still needs inherited stdio.

## Client compatibility

The integration was developed against macOS Codex desktop **26.928.20755** and its bundled CLI **0.159.0**. That client declares conversation entrypoints from tool `_meta["openai/ui"]` and forwards the calling task through `_meta.thread_id` and `_meta.threadId`. These client metadata aliases are validated per request; anonymized `openai/session` and process environment are not task bindings. Older clients without the native extension can still use `show_codex_trajectory` with an explicit session ID.

Native registration follows [OpenAI's extension documentation](https://developers.openai.com/plugins/build/extensions). The viewer uses the standard MCP Apps handshake and also supports the [legacy host bridge](https://developers.openai.com/plugins/reference).

Legacy hosts can supply `window.openai.toolOutput` before viewer startup. The viewer initializes once and reads preferences once for that initial result, including when full details were previously enabled.

Preloaded results also render when the host omits the legacy `callTool` interface. Native initialization validates the negotiated MCP Apps version; failed or unsupported handshakes leave an already rendered trajectory visible, and delayed results can recover an initial loading error.

旧宿主可以在页面启动前提供 `window.openai.toolOutput`。查看器只初始化一次，并为该初始结果读取一次偏好；此前已启用全文详情时也遵循这一行为。

宿主未提供旧版 `callTool` 接口时，预先提供的结果同样可以显示。原生初始化校验协商后的 MCP Apps 版本；握手失败或版本不受支持时保留已经显示的轨迹，延迟到达的结果可以恢复初始加载错误。

## Local verification

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run mypy --no-incremental --platform win32
uv run pytest --cov --cov-report=term-missing
RUN_UI_TESTS=1 uv run pytest plugins/codex-trajectory/tests/test_ui.py -q
uv run python scripts/validate_release.py
uv run python scripts/smoke_mcp.py
```

The browser suite exercises the native handshake, live records and quota updates, theme/locale changes, exact selection fallback, host teardown, history pagination, and legacy display compatibility in local Chromium. It also checks that stopping controls are absent and no task message is sent. This harness is separate from the installed Codex UI.

For a locally installed candidate, use its actual plugin root and an exact task ID:

```sh
uv run python scripts/smoke_mcp.py \
  --plugin-root /absolute/path/to/installed/codex-trajectory \
  --local-session EXACT_TASK_ID
```

This starts the installed plugin's declared launcher, verifies its MCP discovery and self-contained resource, and reads the selected real task in safe-summary mode. It does not start a model turn or alter task history. Native menu visibility and clicking in the desktop app require a separate UI check; protocol and harness success alone do not establish that check.

## Upgrade from the injected viewer

Install the candidate from a local marketplace for development, then fully quit and reopen Codex before reopening an existing task. Installing new files or refreshing the menu does not replace a task's already-running MCP process. Preserve the existing marketplace configuration if you plan to restore the released plugin afterward.

### Log exceeds the read limit

The panel reports an oversized log separately from missing local history. Its error shows the log size and current 512 MiB limit, and offers **Read limit (GB)** with **Enable and retry**. A 2.84 GB task suggests 3 GB. The user can choose a sufficient value up to 64 GB; it applies to that task in the current panel, including refresh, history, full details, and live updates. Reopening the panel restores the default. Explicitly enabled large reads can wait up to ten minutes. File checks, per-line limits, and bounded returned pages still apply, and source logs are untouched.

A watcher launched by an older installed version can outlive its MCP process. Disable the old plugin-owned setting and retire only the verified old watcher; preserve `session-search-index-v1.json`. Restart Codex once without remote-debugging arguments to discard injected controls. The new runtime does not restore old watcher state and needs no debugging port.

### Menu opens but the app cannot be displayed

After reinstalling, an existing task can still use the old MCP process while the menu advertises the new `open_codex_trajectory` tool. The desktop log then reports `Unknown tool name.` for that tool. Reading the UI resource can also report `Internal server error.` if the old process's plugin directory was retired during installation.

Confirm the running plugin process's working directory, rather than relying only on installed files or a successful smoke test started in a new process. Fully quit Codex with **Cmd+Q**, reopen it, and return to the task before clicking the native entry again. Local plugin reload guidance is also covered in [OpenAI Docs](https://developers.openai.com/plugins/build/plugins).

## 本机验证记录（2026-09-30）

- macOS 后端测试 309 项通过，Python 总覆盖率 93.02%；原生宿主、兼容桥接与界面测试 23 项通过。
- 格式、lint、macOS/Windows 类型检查、发布元数据、冻结 schema 和 Windows 启动器源码/二进制一致性检查通过。
- ZIP/TAR 内容检查以及解包后的真实 MCP 启动器 smoke 通过。
- 本机已从当前工作区安装候选版；安装资源与源码一致，真实任务安全摘要读取通过。内置 App Server 实际发现了带有 thread entrypoint 的应用专用工具。
- 已退出经过进程路径确认的旧 watcher，并清理旧 CDP 状态；搜索索引保留。已有未提交的隐私、计价与解析修复保留。
- 后续实际点击排查确认：已有任务仍使用旧版 `0.4.1` 的 MCP 进程，其工作目录已被安装流程移走；日志同时出现原生工具未知和页面资源读取失败。新安装版本的独立进程再次通过当前任务读取与页面资源 smoke。已有任务需完整退出并重开 Codex 后重新建立插件连接。

桌面应用内的实际菜单点击尚未自动验收。重新打开任务或重启 Codex 后，可在右侧工具菜单打开原生入口；这里的协议与 Chromium 界面测试不等同于操作 Codex 本体。
