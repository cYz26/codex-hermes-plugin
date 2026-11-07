# Codex 执行、Hermes 学习：v0.22.7 实施与验收

后台核心使用官方 **Hermes v0.22.7 / `b200f5bc60e31588ba2cb60e288029c4a54ba5fa`**＋Learning API v1 补丁。学习模型使用 **Max**，复用 Hermes App 登录和独立的学习目录。外围 0.2.2 的运行与用量证据见 [发布验收](./docs/RELEASE-0.2.2.zh-CN.md)。


0.2.0 全局部署后，0.2.1 通过两项目的 App 自动学习与召回。下列 0.1.6 路径和单项目证据保留为历史记录。[全局启用记录](./docs/GLOBAL-ROLLOUT.zh-CN.md)

## 0.1.6 运行边界快照

| 项目 | 当前值 |
| --- | --- |
| 核心源码 | `/Users/cy/Dev/agents-dev/hermes-learning-core`，分支 `feat/learning-api-v1-v0.22.7` |
| 官方基线 | `v0.22.7 / b200f5bc60e31588ba2cb60e288029c4a54ba5fa` |
| 补丁与校验 | [补丁](./patches/native-learning-api-v1.patch)、[manifest](./patches/manifest.json)；14 文件，逐字节重放通过 |
| 后台隐藏入口 | `/Users/cy/.hermes/profiles/.codex-learning-pilot`，指向独立数据目录的目录链接；2026-11-04 隐藏，不再是 App 可枚举的 profile 名称 |
| 独立学习数据 | `/Users/cy/.hermes-codex-learning/pilot` |
| 认证来源 | Hermes 原生 profile fallback → `/Users/cy/.hermes/auth.json`；未复制凭据、未创建学习 profile 的 `auth.json` |
| 独立解释器 | `/Users/cy/.hermes-codex-learning/runtime-v0.22.7/bin/python`，保持 venv 路径 |
| 后台服务 | `com.cy.hermes-codex-learning.pilot`，独立 Unix socket 与持久队列 |
| Codex 插件缓存 | `/Users/cy/.codex/plugins/cache/hermes-learning-local/hermes-learning/0.1.6`；18 个运行文件与源码一致，A／B 已实际连接 MCP |
| 采集范围 | `/Users/cy/Dev/agents-dev/hermes-plugin1102` 的主聊天，包含 `pilot-project`；只收后续 hooks，不回填旧转录 |

现有 Hermes 安装源码目录 `/Users/cy/.hermes/hermes-agent` 的 HEAD 为官方 v0.22.7；其 `auth.py`、`auth_codex.py` 和 `credential_pool.py` 与本项目基线逐字节一致。只通过原生认证库共享账号状态，不引入旧定制版源码、配置、记忆、技能或更新器。现有 App 未被替换、重启或打补丁；新增 profile 标记 `gateway.standalone: true`，原生 gateway 的服务集合排除这份学习数据。

核心补丁修改 4 个官方文件，新增 6 个通用学习模块、4 个测试文件。核心仍为通用 `ctx.learning(version=1)`，Codex hooks、队列、空闲补充、MCP、通知及安装逻辑均在外围。核心工作区尚未提交 Git，没有远程推送。

## 认证复用与数据隔离

启动入口保留原生 `root/profiles/name` 路径，数据读写绑定其解析后的独立目录。CLI、launchd 和验证脚本使用同一启动环境，避免路径解析提前消除 profile 身份。目录链接必须解析到配置的学习 home，否则拒绝启动。

复用现有 Hermes 登录不经过 Codex App 凭据，也不复制 OAuth token。刷新由 Hermes 的原生跨进程锁与源感知回写负责；模拟刷新回归证明新 token 写回原库，保持根账号的 provider 选择，学习 profile 不产生认证副本。原生只读状态检查显示 `logged_in=true`、认证根为 `/Users/cy/.hermes`。`scripts/bridge login` 在已登录时直接返回状态，不再次发起授权。

真实模型验收前后，原 App 的 **437 个记忆／技能文件**的路径、大小与修改时间指纹一致；学习目录中产生新记忆、技能、会话、usage 与发布副本。该观察对应验收时间窗；临时 profile 回归另验证了配置、记忆、技能、journal 及 gateway 服务集合隔离。

## 外围 0.1.6 与真实 App 续验

用户已确认 Hermes App 的 Sessions、Bots 不再展示后台入口。外围 0.1.4 修复了 App MCP 字面路径错误；0.1.6 又完成 App 委派输入的真实身份关联、逐聊天空闲、内部建议排除、显式代理环境等修复，20 项外围回归通过，已安装 18 个运行文件与源码一致。本聊天、A、B 均已实际调用 MCP。

用户授权的 A 聊天实际 RED→GREEN，6 项业务测试通过；原任务及只读复核的真实 hooks 已入库。首次复盘因 launchd 缺少代理而连接超时，协作取消后为 interrupted、0 写入、0 不明。配置现有代理后，A 新回合按既定空闲规则自动复盘成功，10 次原生写入涉及两个技能，没有手动总结或调用 review。

B 聊天实际加载技能正文及 A 新增参考资料，完成扩展，15 项业务测试通过。插件升级曾使 7 项 hooks 变为 modified，B 首轮没有 hook 事件。用户已重新信任，两个 B 只读回合的 5 次真实工具调用、2 次 Stop 已投影；正文和参考文件有原生 body_delivered，版本及哈希与 A 完全一致。单项目 App 闭环通过，首轮缺口和显式版本核对的性质保留，不回填历史事件。B 随后也按原 idle_flush 规则完成自动学习：4 次调用、3 次技能修订；两轮均未手动触发复盘。

具体回执、版本、tokens、信任诊断和恢复步骤见 [App 验收](./docs/APP-VALIDATION.zh-CN.md)。以下 0.1.3 原生／stdio 结果保持历史来源，不重标为 App 证据。本轮没有修改核心补丁。

## 原有回归与真实模型验证

| 验证 | 最终结果 | 证据 |
| --- | --- | --- |
| 官方 v0.22.7 原版相关回归 | 19 文件，328 通过，0 失败 | [日志](./evidence/v0.22.7/native-baseline.log)、[来源校验](./evidence/v0.22.7/native-baseline.json) |
| 带补丁的原生与扩展回归 | 23 文件，343 通过，0 失败 | [日志](./evidence/v0.22.7/native-tests.log) |
| 认证专项 | 65 通过，包含在综合回归范围内 | [日志](./evidence/v0.22.7/auth-tests.log) |
| 外围桥接 | 9 通过，0 失败 | [日志](./evidence/v0.22.7/bridge-tests.log) |
| 完整原生检查 | 14 项通过；health 0 blocking / 0 advisory | [日志](./evidence/v0.22.7/native-check.log) |
| 新增代码与外围 lint | F/E9、diff 空白检查通过 | [质量记录](./evidence/v0.22.7/quality.json) |
| 实际账号认证复用 | 原生状态已登录，未新增授权 | [认证记录](./evidence/v0.22.7/auth-reuse.json) |
| 真实模型原生复盘 | completed，4 次成功写入，0 暂存／拒绝／不明 | [原始结果](./evidence/v0.22.7/real-learning.json)、[日志](./evidence/v0.22.7/real-learning.log) |
| 原 App 学习数据隔离 | 真实验证期间指纹未变，profile 无认证副本 | [隔离记录](./evidence/v0.22.7/data-isolation.json) |
| 已安装 MCP → 新服务 | 握手、工具列表、状态、召回、完整技能正文与内容 hash 通过 | [MCP 记录](./evidence/v0.22.7/installed-mcp-summary.json) |
| 服务重启 | 新解释器启动，历史原生 usage 恢复，状态 idle，无错误 | [服务快照](./evidence/v0.22.7/service-summary.json) |

原版对照使用临时源码副本，还原所有已修改官方文件并移除新增文件；验证 import 来自该副本后，用同一隔离依赖环境和原生 runner 执行。不是把修改版的结果当作原版结果。完整检查缺失的稀疏 inventory 已补齐，未弱化检查器，初次缺失记录仍保留。

真实复盘通过现有订阅账号调用 `gpt-6.1-sol`，profile 为 Max，执行 Hermes 原生 `codex_responses` 复盘循环。它新增 **1 条记忆**，创建并两次修订 **1 项技能 `agent-integration-acceptance`**；4 次成功写入不是 4 个不同技能。技能创建的原生 authoring advisory 保留在工具结果中，没有把 advisory 改成成功验证或手工覆写学习产物。

这次输入明确标记为 **synthetic acceptance fixture**，并显式请求 idle-flush；它验证真实认证、模型、原生工具及写入链路，**不能证明真实 Codex App hooks 已采集或自动触发**。已安装 MCP 的 stdio 测试同样不冒充 App 内工具调用。

## 本次修复

- 保留启动 profile 的路径拼写，数据仍指向独立 home；对错误目录链接拒绝启动。外围新增 3 项测试先红后绿。
- 原生批量 `skill_manage` 的成功结果提升真实技能名称和 action，便于回传与召回；兼容旧 v1 收据的 `operation.name`，不改写历史记录。2 项新增回归先红后绿。
- owner 重启后，通过原生 SessionDB 恢复已持久化 usage，读取账本不创建 Agent 或调用模型。新增回归先红后绿；实际重启后仍为 6 次 review API 调用。

## 历史 Token 与验收范围

真实复盘原生账本记录 **6 次 API 调用**，输入 16,300、输出 3,252、cache-read 68,608、reasoning 2,419。上述是原生分项口径，不把 attempt 和 native ledger 相加；没有据此另算一个总 token 数。原生 `estimated_cost_usd=0.0` 不代表订阅调用免费或实际费用为零，实际费用／额度变化尚未知。来源 Codex 执行与外部 provider usage 不可得时保持 `null`。

实际重启及已安装 MCP 读取后，原生 review 次数仍为 6，读取过程没有增加复盘调用。通知仍使用程序模板；技能正文按需加载。正文交付、采用、验证成功仍是不同事实。

A 的 4 次复盘 API 调用用量为 input 41,833、output 5,938、cache-read 103,424、reasoning 3,762。B 后续采集与原生交付通过，自动复盘用量为 input 23,858、output 3,931、cache-read 59,776、reasoning 2,517，3 次操作修订同一个技能。用户确认 0.1.6 的聊天提示和 B 的系统通知可见。0.2.2 的点击回执及同任务成本使用独立证据。[发布验收](./docs/RELEASE-0.2.2.zh-CN.md)

## 回退与升级

升级前快照位置见 本机 `UPGRADE-CHECKPOINT.json`（本地生成，不入库），快照目录在 `/Users/cy/.hermes-codex-learning/backups/`，权限 0700。旧 v0.22.6 解释器保留在 `/Users/cy/.hermes-codex-learning/runtime/bin/python`；旧源码补丁、配置、数据和 LaunchAgent 定义已留存。没有复制或回退根账号认证文件，避免回放已轮换 token。

回退先停止本 pilot，按快照恢复源码、学习数据及服务定义，验证数据版本后再启动。Hermes 后续升级仍以官方稳定版为基线重放本项目补丁，执行原生与冻结合同测试后切换。Codex 更新主要复查外围 hooks、身份、MCP、提示和信任；不承诺任意官方新版本无条件兼容。

历史研究探针 `HERMES-CODEX-PROBE.json` 保持原字节，SHA-256 `79c042e1e6d3acfd91adf9a882d568197312efda248a0384bf738ee23fd03142`。旧实现记录见 [v0.22.6 archive](./docs/archive/IMPLEMENTATION.v0.22.6.md)。
