# Codex 执行、Hermes 学习：整体方案

保存日期：2026-11-04。目标基线为官方 **Hermes v0.22.7 / `b200f5bc60e31588ba2cb60e288029c4a54ba5fa`**，外围插件 **0.1.3**，Learning API **v1**。本文整理当前实现；实际结果以 [实施记录](../IMPLEMENTATION.md)及 [验收摘要](../ACCEPTANCE.json)为准。

## 目标与分工

用户继续在 Codex App 中执行任务。真实 hooks 自动采集执行证据并持久化，后台 Hermes 负责复盘、记忆、技能和维护；用户不需要手动要求总结。成果通过聊天提示、系统通知和后续任务的上下文／技能读取回到 Codex。

```mermaid
flowchart TD
    A[Codex App 执行任务] --> B[真实 hooks：会话、回合、工具身份]
    B --> C[持久事件箱与独立后台队列]
    C --> D[Hermes Learning API v1]
    D --> E[原生 unattended review]
    E --> F[原生记忆、技能、写入保护与技能锁]
    D --> G[原生 Curator 与 provider 生命周期]
    F --> H[真实结果与版本化学习产物]
    H --> I[聊天提示与 macOS 通知]
    H --> J[增量上下文与 MCP 按需读取技能]
    J --> A
```

| 组件 | 职责 |
| --- | --- |
| Codex App | 业务执行、任务交互和最终业务验证；核心不改 |
| Codex 插件 | hooks 采集、增量反馈、MCP 读取、技能加载指引 |
| 独立后台服务 | 持久队列、投递、去重、空闲补充、通知和发布 |
| Hermes 核心扩展 | `ctx.learning(version=1)`，管理观察会话、原生学习、真实结果与恢复 |
| Hermes 原生机制 | 提示词、模型路由、记忆／技能工具、暂存、审批、技能锁、Curator、provider 收尾 |

## 官方版本与补丁形式

本工程是“官方源码＋可重放学习补丁＋外围插件”。独立运行目录不等于另行维护一套完整 Hermes 发行版，但实际运行代码包含本项目扩展；未修改的官方 App 不能只安装外围插件就获得当前 `ctx.learning` 接口。

核心补丁修改 4 个官方文件，新增 6 个通用学习模块和 4 个测试文件。Codex 专属逻辑保持在外围，核心不导入本项目的队列、通知或 Codex 适配代码。完整 Hermes 源码由官方仓库提供，本仓库保存 [补丁](../patches/native-learning-api-v1.patch)和 [校验清单](../patches/manifest.json)，不包含旧定制版源码或更新器。

当前补丁已在固定官方基线上重放，并核对 14 个文件的完整内容。官方新版本仍需重新检查、重放和回归，不能仅凭版本号增加就承诺兼容。

## 认证复用与数据独立

学习服务使用原生 named profile 机制继承已有 Hermes 的 OpenAI 登录。目录入口保留 `root/profiles/name` 的拼写，指向专用学习数据目录；核心在每次调用中绑定该数据目录。

```text
现有 Hermes 根目录
  auth.json                         现有认证库，由原生机制管理
  profiles/codex-learning-pilot  →   独立学习 home

独立学习 home
  config.yaml                       新配置，Max、原生 review、独立 gateway
  memories/ skills/ state.db         专用学习数据
  learning/                         v1 journal、发布产物及收据
  bridge/                           事件箱、状态快照、socket 与通知记录
```

不复制 access/refresh token，也不读取 Codex App 的认证文件。刷新由 Hermes 的跨进程锁和源感知回写执行；共享登录的 profile 不各自保存一份会分叉的旋转凭据。外围只调用原生只读认证状态接口，导出不含凭据的字段。

学习 profile 配置 `gateway.standalone: true`，主 gateway 的服务集合排除它，专用 launchd 服务拥有其学习生命周期。现有 Hermes 的配置、记忆、技能和会话不作为新学习 profile 的初始化来源。

当前部署的数据目录为 `/Users/cy/.hermes-codex-learning/pilot`；原生入口为 `/Users/cy/.hermes/profiles/codex-learning-pilot`。这些是本机部署记录；安装到其他机器时使用 [README](../README.md)中的显式路径参数。

## 模型配置

```yaml
model:
  provider: openai-codex
  default: gpt-6.1-sol
  api_mode: codex_responses
agent:
  reasoning_effort: max
auxiliary:
  background_review:
    enabled: true
    provider: auto
    model: ""
```

模型和认证属于 Hermes。`auto` 继承这个学习 profile 的模型，并不跟随 Codex App 当前任务模型。使用原生 `codex_responses` 工具循环，使复盘中的记忆和技能工具由 Hermes 执行。更换 provider、endpoint 或模型应通过原生配置完成，并重新验证真实工具调用与写入。

## 自动触发与持久恢复

- 保留原生回合／工具阈值，不将每条事件投递都变成一次模型调用。
- 只按真实 host、project、chat、turn、tool-call 身份计数和去重；缺失身份时记录覆盖缺口，不凭内容相似度猜测工具次数。
- 明确完成且存在未审阅证据时，空闲 120 秒可请求补充复盘；至少间隔 600 秒，并发为 1。
- 长命令仍运行时不视为空闲；同一批证据不因原生阈值和空闲机制重复复盘。
- Curator 保留原生到期与空闲规则，不伪造技能活跃度。

外围事件先落盘再投递。核心 journal 原子保存观察、计数、intent 和 receipt；原生转录以稳定 message UID 投影。执行结果区分 applied、staged、refused、uncertain 与完成状态。

进程在投影或复盘期间退出时，恢复后标为 uncertain／unknown，不盲目重跑。journal、原生 DB、文件和远端 provider 不是一个跨存储事务。关闭执行原生取消、会话收尾与 provider drain；协作取消不表示已经回滚写入。

## 反馈与后续召回

用户提示根据真实结果生成。hooks 的 `systemMessage` 在支持的安全点显示，后台空闲完成可派发系统通知；系统是否展示和 App 的具体提示样式分别验收。无新增成果时默认不弹系统通知，失败和暂存也不显示成已生效。

模型通过有作用域和版本的增量上下文感知成果，单次宿主上限约 256 tokens。技能正文通过 MCP 按需加载，保留正文、资源 hash、版本和原生 readiness 信息。批量写入保留技能身份，旧 v1 收据也能召回更新后的技能。

聊天、模型上下文和系统通知分别消费，重启避免重复播报。通知使用程序模板，不为通知额外调用模型或唤醒 Codex。正文交付、采用和验证有效是不同事实；只有相应证据才能记录相应阶段。

## 与原生 Hermes 的一致性和差异

复盘由 Hermes 原生 unattended fork 执行，复用其提示词、模型路由、工具白名单、输入预算、写入保护、暂存规则和技能锁。模型产生的技能仍可以继续修订，维护交给原生 Curator。

区别在于业务历史来自 Codex：观察会话需要规范化外部工具结果；Hermes 没有执行主任务，不能保证复用 Codex 的缓存；空闲补充增加了原生阈值之外的触发。Codex 侧提示、技能交付和使用证据由宿主适配负责。这些差异不能被描述为与原生交互完全等价。

v1 支持边界为 macOS／POSIX、in-process 插件、单 home 单 owner、独立单项目主聊天。子 agent、跨机器部署和多 owner 并行尚未验收。

## Tokens 与费用

采集、排队、去重和程序通知不调用模型。额外消耗主要来自跨运行时的缓存差异、空闲补充，以及成果返回 Codex 的输入上下文。原生复盘本就消耗 tokens，不能将其全部算成桥接新增开销。

保留原生分项口径，区分来源任务、复盘与其他辅助调用，不把 attempt 和 native ledger 重复相加。不可获得的 usage／实际费用为未知，不填零；原生 OAuth 路由的 `estimated_cost_usd=0` 不代表实际免费。当前未完成相同任务、相同触发条件下的原生对照成本实验。

## 已有证据与剩余工作

| 范围 | 已验证结果 |
| --- | --- |
| 官方 v0.22.7 原版相关回归 | 328 项通过 |
| 加补丁后的原生和扩展回归 | 343 项通过 |
| 外围桥接 | 9 项通过 |
| 原生完整检查 | 14 项通过，health 无阻断或 advisory |
| 真实 provider | 复用现有登录，6 次原生 review API 调用，新增 1 条记忆、创建并修订 1 项技能 |
| 数据隔离 | 验收时间窗内原 App 437 个记忆／技能文件的元数据指纹一致 |
| 已安装 MCP | 状态、召回、完整技能、版本和内容 hash 通过；重启后 usage 恢复，读取未新增复盘 |

真实模型输入明确标记为验收 fixture；stdio MCP 测试也不冒充 App 内调用。真实 Codex App hooks 信任、聊天 A 自动学习→聊天 B 召回、提示样式、系统通知可见性、完整 App 生命周期及质量／成本对照仍待验收。

证据按版本保存在 [evidence](../evidence/README.md)，原始私有运行回执本地保留；公开摘要不携带 live delivery token。历史研究和旧探针列于 [文档索引](./README.md)，不替代当前验收。

## 升级、回退与维护

Hermes 更新先停止本学习服务并保留一致性快照，在新官方稳定版候选中重放补丁，运行原生及插件合同回归，再切换解释器和服务。数据兼容性与回退单独验证，旧运行时在验证完成前保留。

回退学习数据和服务定义时不恢复根账号的旧认证副本，避免回放已轮换 token。Codex 更新主要验证 hooks 字段、身份、信任、提示和 MCP；核心补丁按 API 行为合同维护，不依赖旧定制版更新器。

本仓库只保存工程与可提交的验证材料。运行时、账号认证、真实学习数据、回退快照与机器私有回执不进入 Git；整体工程推送不等于重新部署或新增 App 验收。
