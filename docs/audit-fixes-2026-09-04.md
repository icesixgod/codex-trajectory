# 全量审查问题修复与复测记录

日期：2026-09-04。范围：当前工程源码、随包插件与 Skill、测试、发布校验和说明文档。本轮未提交、发布、更新实际安装的插件，也未修改真实 Codex/CDP 配置。

## 已修正的问题

| 模块 | 修正结果 | 回归验证重点 |
| --- | --- | --- |
| 会话发现与搜索 | 压缩日志按解压后的逻辑偏移搜索；谱系共享读取预算；每次请求复用发现索引；精确元数据 ID 优先于文件名别名 | 冷缓存压缩搜索、谱系偏移、读取预算、文件替换/链接攻击、ID 冲突 |
| 私有搜索索引 | 持久化 LRU 访问顺序；跨进程锁内合并本次修改；不以旧快照覆盖其他进程更新；安全保存非标准 Unicode | 两个索引实例交错写入、淘汰顺序、孤立代理码点、安全文件检查 |
| 任务选择与隐私 | 默认只读活跃任务；`latest` 明确表示最近修改的活跃任务而非调用任务；全文必须给出明确 ID；披露搜索索引落盘及标题敏感性 | 默认参数、全文拒绝隐式 ID、元数据匹配、工具说明与随包 Skill |
| 轨迹投影 | 累计 Token 按计数器差值累计并处理重置；缺失调用 ID 不再误关联；迟到输出不覆盖权威完成时刻 | 缺失/不一致 Token 样本、重复与重置、调用关联、迟到标记与完成时长 |
| 资源上限 | 投影缓存同时限制条目数与体积；记录页主动缩小并保留有效游标；浏览器历史采用有上限的滑动窗口 | 5 MiB 记录页、8 MiB 响应、24 MiB 缓存、5,000 条/20 MiB 浏览器历史与 v2 Schema |
| MCP 协议 | 初始化后才允许业务调用；通知不执行请求专属方法；控制消息不被慢业务或满队列阻塞；拒绝重复进行中 ID | 真实 stdio smoke、队列饱和期间 ping、取消排队请求、异常脱敏、参数错误 |
| 浏览器服务与交互 | 生命周期幂等；异常返回固定错误；HEAD 无响应体；请求有超时；加载失败可重试；未绑定任务不能 Stop | 本地 HTTP 与真实 Chromium 验收、初始失败重试、绑定与操作结果确认 |
| 大数据与窄屏 UI | 避免大数组展开导致崩溃；明确局部统计口径；选择失败恢复；稳定记录锚点保持滚动位置；窄屏与键盘可操作 | 160,000 条记录时间域、历史窗口、400/600 px 布局、键盘操作、滚动行为 |
| CDP 与 Stop | WebSocket 严格校验帧和握手；保留握手后首帧；目标过滤后限量；引导读取真实活动 turn；先核实明确任务/turn 再操作 Goal，不自动改停新 turn | 合并首帧、非法帧、总时限、目标上限、过期 turn 不修改 Goal、Goal API 不支持时拒绝 |
| 状态文件与 watcher | 拒绝畸形配置和重解析文件；心跳需匹配活进程及锁所有者；修复停用状态下旧 watcher 升级清理 | 非法配置、链接/重解析点、失效心跳、停用升级、注入异常 |
| Windows 认证与启动器 | 验证有效签名、允许的签名者、注册包发布者及安装路径；修复 C 启动器长度竞态；固定工具链重建 EXE | 认证拒绝路径、Windows 类型检查、二进制格式与哈希、两次构建字节一致；原生联调另见限制 |
| 发布与测试 | 同时冻结 v1/v2 Schema；归档必须包含搜索索引和构建脚本；解包 smoke 使用脚本声明的依赖；Windows canary 核对实际安装候选内容及 watcher 版本 | ZIP/tar.gz 内容一致性、四个 Python 版本的隔离解包 smoke、插件与 Skill 校验 |

## 复测结果

- macOS / Python 3.13.9，启用 Chromium UI 的完整测试：**442 passed、5 skipped**，耗时 87.24 秒。
- 完整测试的 Python 分支覆盖率：**86.67%**；核心包 **89.00%**。所有现有独立覆盖率门槛通过，未降低门槛。
- Python **3.10、3.12、3.14** 的隔离非 UI 测试分别为 **400 passed、5 skipped、42 deselected**；这些结果是同一 macOS 主机上的运行时兼容性验证，不代表三个操作系统均已验证。
- Ruff 格式与 lint、默认平台及 Windows 平台 mypy、发布校验、插件与 Skill 结构校验通过。
- 从当前未提交源码快照生成 ZIP 与 tar.gz，核对安全条目及内容一致性，解包后在 Python **3.10、3.12、3.13、3.14** 执行 MCP stdio smoke 均通过。未使用旧 HEAD 代替修复后的源码。
- Windows 启动器两次独立构建字节一致，SHA-256：`eae4f330c46e521624f26362a7062c1fc0db44ee109aa6c9acfc3f242f36cbd6`。
- 修正了两个 UI 测试的等待条件：等待 Stop 操作完成后的状态确认，而非仅等待请求发出；未删掉关键行为断言。

## 尚未在本机验证的边界

5 项跳过均与 Windows 原生能力相关：锁升级、进程句柄、TCP 所有者、EXE 启动器执行，以及真实安装 Codex 的 canary。本机为 macOS，不能将模拟认证测试、交叉编译或 Chromium 测试视为这些项目已通过。CI 中保留原生 Windows 测试；发布前仍须按 CONTRIBUTING 的步骤运行候选插件 canary，尤其核实真实安装包的签名者、发布者和安装目录匹配。

Stop 通过多个 App Server 请求顺序完成检查、Goal 暂停和 turn 中断，不是服务端原子事务。当前实现拒绝已知过期/未绑定请求，不会自动重新绑定另一个 turn；跨请求之间的服务端状态竞态仍需由上游原子接口才能完全消除。

## 常用复测命令

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run mypy --no-incremental --platform win32
RUN_UI_TESTS=1 uv run pytest -q --cov --cov-report=term-missing
uv run --isolated --python 3.10 pytest -q -m 'not ui'
uv run --isolated --python 3.12 pytest -q -m 'not ui'
uv run --isolated --python 3.14 pytest -q -m 'not ui'
uv run python scripts/validate_release.py
uv run --no-project --script scripts/build_windows_launcher.py --check
uv run --no-project --script scripts/smoke_mcp.py
```
