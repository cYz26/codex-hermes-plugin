# 在 Codex App 中自动运行 Hermes 的自进化体系

> 2026-11-03 最新约束：Codex 执行任务且核心不改，Hermes 侧允许扩展和重构。本文保留为此前源码研究和内部入口实验记录。当前建议将这些内部能力提取为[Hermes 正式外部轨迹学习服务](/Users/cy/Dev/agents-dev/hermes-plugin1102/HERMES-CODEX-PLAN.md)，由 Codex 插件调用；实验中的私有复盘调用和外部直接写 SessionDB 不作为产品接口。历史收据支持部件可行性，不代表新服务、桌面安装或真实模型学习效果已经验收。

2026-11-02 · macOS · Hermes Agent v0.22.5（`bf0203a794a0`）· 桌面 App 自带 Codex CLI `0.166.0-alpha.5.1`

**建议实现一个本地 Codex 插件和一个独立的 Hermes 学习进程。** 你照常在 Codex App 创建任务、改代码和运行测试。每次任务推进，插件自动记录这一轮发生了什么。后台把完成的回合交给专用 Hermes profile，运行原生记忆、技能复盘及维护。下一条 Codex 聊天自动得到当前项目的记忆和技能。日常使用没有“让 Hermes 总结”、打开第二个界面或逐条批准普通新记忆的步骤。

建议先用一个项目专属的学习 profile 验证，后续按允许共享经验的范围扩展。Hermes 的内建记忆和技能按 profile 保存。按已选策略，学习 profile 使用 Hermes 原生自动写入，`memory.write_approval=false`、`skills.write_approval=false`。Hermes 仍执行它自己对破坏性更新的保护。Codex 持有项目执行权，Hermes 学习进程持有该 profile 的经验库。首次启用时需要按 Codex 的产品流程信任本地 hook 定义。后续普通任务自动触发。[OpenAI Docs · Hooks](https://learn.chatgpt.com/docs/hooks) · [Hermes 记忆写入](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) · [Hermes 技能写入](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)

## 已跑通的最小闭环

隔离实验用了桌面 App 自带的真实 Codex App Server、真实命令执行、四个经哈希信任的 hook、临时 Hermes profile、原生 SessionDB 和原生复盘工具。两端模型都换成本地可控制响应的测试服务，以便检查每一次输入和写入。用户任务中没有提及 Hermes 或总结。[实验收据](/Users/cy/Dev/agents-dev/hermes-plugin1102/HERMES-CODEX-PROBE.json)

| 步骤 | 观察结果 |
| --- | --- |
| 聊天 A 执行普通任务 | 用户给出产物命名约定 `QUARTZ-17` 并要求执行 `CHECK-47`。Codex 实际运行命令。`UserPromptSubmit`、`PostToolUse`、`Stop` 自动留下原始用户要求、工具结果和最终答复。预先启动的后台进程自动消费完成事件。 |
| Hermes 原生复盘 | 将用户、助手工具调用、工具结果、助手最终答复四条消息写入同一 Hermes 学习会话。本次试件开启记忆与技能两类复盘，主任务在 Hermes 的额外推理次数为 **0**，复盘模型请求 **3** 次。原生 `memory` 写入一条记忆，`skill_manage` 创建一个技能，技能账本新增一条，普通写入待审数为 **0**。通过 Hermes 会话全文搜索，能从用户、助手和工具三种来源找回证据。 |
| 新聊天 B 自动复用 | 用户只问下一件产物适用什么约定。`SessionStart` 将 Hermes 记忆带入新聊天，Codex 的 `skills/list` 发现发布的 `fixture-artifact-naming`，随后实际读取 `SKILL.md` 的操作步骤。B 给出正确的命名和验证方式。Hermes 原件与试验项目内技能的 SHA-256 一致。 |
| 对照与维护 | 给相同的模型脚本和相同的 B 提问换一个没有学习产物的环境，它取不到约定。另一项原生 Curator 试验模拟经过 15 天和 31 天，观察到技能变旧、归档和恢复，恢复后的正文逐字节一致。独立的 provider 探针也验证了已完成回合的同步回调。 |

这证明 **普通任务结束 → 自动复盘 → 经验落盘 → 新 Codex 聊天读到记忆并使用技能** 在当前版本具有可行的执行路径。试验从 App Server 协议驱动，在一次完成事件上主动开启两类复盘。桌面 GUI 安装、长期自动调度、真实模型对经验的取舍以及跨项目误用率还需要试点数据。一次试验中技能正文被输出额度截断，虽然技能已列出，聊天 B 仍答错了。复验提高读取额度并校验操作步骤后才通过。[实验输入、对照和回执](/Users/cy/Dev/agents-dev/hermes-plugin1102/HERMES-CODEX-PROBE.json)

## 推荐实现

```text
Codex App 聊天 A 执行项目任务
  └─ 生命周期 hook → 本地持久队列 → 专用 Hermes 学习进程
                                   ├─ 保存同一轮的消息、工具结果和来源
                                   ├─ 按条件运行原生记忆与技能复盘
                                   ├─ 同步已配置的记忆 provider，维护技能库
                                   └─ 发布已实际写入的记忆和技能版本
Codex App 聊天 B 启动或提交新问题
  └─ 自动加载匹配的记忆和技能 → 记录实际读取与采用 → 反馈给 Hermes
```

### 1. Codex 自动产生学习材料

用 `UserPromptSubmit` 记录人类输入，用 `PostToolUse` 记录工具身份和有用的结果，如测试命令、退出状态、报错和产物引用。`Stop` 把当前回合写入持久队列后立即返回。`Interrupt` 标记中断，有完整终态后才按成功或失败归档。`Stop` 是一轮结束，`SessionEnd` 是会话结束或卸载，两者职责不同。大段输出可按需从 App Server 的 `thread/turns/list`、`thread/items/list` 补齐。hook 的 `transcript_path` 可以为空，官方也提示其文件格式会变。[OpenAI Docs · Hooks 字段和生命周期](https://learn.chatgpt.com/docs/hooks) · [App Server 的聊天与消息接口](https://learn.chatgpt.com/docs/app-server)

按 `host + projectId + thread.id` 绑定到一个 Hermes profile 和学习会话，再按 `turn.id + item/event id` 去重。Codex 的 `thread.sessionId` 可被整棵分叉树共享。父聊天、分叉、子代理各自保留来源，归属未确定的事件等待后台核对。区分人类原话、助手总结、工具结果和上次注入的记忆，避免同一条 AI 总结被反复采集为人类偏好。只采集已启用范围的任务，过滤凭据，沿用聊天的隐私和记忆选择。[OpenAI Docs · 分叉身份](https://learn.chatgpt.com/docs/app-server) · [聊天级记忆设置](https://learn.chatgpt.com/docs/customization/memories)

### 2. Hermes 接收一个已经执行完的外部回合

最值得复用的是 Hermes 自己接 Codex App Server 的源码。它已将 Codex 事件转换为 Hermes 消息，保存消息、统计工具活动、同步外部记忆，并在满足条件时启动后台复盘。现有入口假设该轮由 Hermes 发起。建议给专用学习进程增加一个有版本的小接口，例如 `observe_completed_turn`，输入已完成回合及来源，复用这些逻辑。这个接口是拟议的新代码。本机已有的 `coding` profile 走的是 Hermes 发起、Codex 执行的方向。[官方运行时说明](https://hermes-agent.nousresearch.com/docs/user-guide/features/codex-app-server-runtime) · [事件投影源码](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/transports/codex_event_projector.py) · [消息持久化与回合收尾](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/codex_runtime.py#L666-L724)

在 v0.22.5 中，可参考 `_persist_projected_messages` 和 `_finish_codex_turn` 的分工。试件用 `SessionDB.append_messages_batch` 入库，随后直接调用 `AIAgent._spawn_background_review`，保留其无人值守模式。以下划线开头的方法是内部 API，发布适配器时需绑定版本，更新 Hermes 时重跑同一闭环。

1. **保存回合。** 使用 Hermes SessionDB 的建会话及批量追加消息方法，保留用户、助手和工具角色、工具调用关系、来源 ID 以及结果状态。单独运行原生快照复盘时，实测父会话消息数为零。显式入库后才能依靠原始历史做跨会话搜索和恢复。[原生消息存储](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/hermes_state_messages.py) · [Hermes 会话搜索](https://hermes-agent.nousresearch.com/docs/user-guide/sessions)
2. **推进原生学习节奏。** 每个完成回合都入库。按真实用户回合和 Hermes 投影认可的工具活动计数，本机配置分别为 10 和 15。触发时将真实消息快照传给后台复盘，沿用启用状态、工具可用性、输入预算和原生写入保护。很短的任务可能在达到阈值前结束。建议为两种情况自动安排一次合并复盘，一是用户纠正后已有验证结果，二是会话空闲后仍有未复盘材料。同一份材料只排一次。实验为了可重复性只验证了触发后的执行路径，日常节奏与短会话策略需分别验收。[用户回合计数](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/turn_context.py#L750-L767) · [原生复盘入口](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/run_agent.py#L757-L824)
3. **单独转发已配置 provider 的生命周期。** 原生复盘 fork 会跳过外部记忆初始化。在试验中，单跑 fork 时本地记录型 provider 收到零次回调，从父代理调用 `_sync_external_memory_for_turn` 转发已完成回合后，收到 `sync_turn` 和 `queue_prefetch`。若启用了检索型 provider，还应按真实的会话开始、提问、压缩、分叉和结束边界转发其支持的事件，并把检索结果带回 Codex。外部服务按专用 profile 配置启用。[父代理的外部记忆同步](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/run_agent.py#L892-L940) · [Hermes Memory Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers)
4. **记录结论并处理重试。** 区分已入库、复盘中、无可学内容、已写入、原生待审、已发布和结果未知。原生通知只在有可报告动作时出现，worker 要记录实际运行结束，空结果也要能结案。进程崩溃后先按源事件、账本和产物版本核对，再决定是否重试。账号断开或学习进程离线时，Codex 主任务可以照常完成。[后台复盘结束与通知源码](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/background_review.py#L1211-L1305)

**部署首选**是让 macOS 用户级学习进程独立消费队列，使用固定版本的 Hermes 运行环境和专用 profile。它也可成为 Hermes 的扩展，但应通过受支持的扩展契约或一个很薄的版本适配层调用内部方法。当前本地 gateway 可持久受理普通代理任务，`session.create` 的公开协议却不接受任意外部历史作为初始历史。HTTP Runs 也有自己的会话所有权，给普通 `input` 发送 `/refine` 不会执行原生命令。v0.22.5 会在有限执行上下文跳过自动后台复盘，因此反复启动 `hermes chat --oneshot` 无法依赖这一机制。如果只允许依赖公开接口，后台可向普通、持续的 gateway 会话提交一轮学习任务，代价是多一轮模型解释，且 Hermes 观察到的是转交后的消息。直接让后台自动发送 `/refine` 还会进入 `explicit=True` 的显式复盘路径，与原生无人值守语义有别。这两条限制是推荐源码适配层的原因。[显式复盘的实现](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/gateway/session_refine.py#L67-L95) · [Gateway 建会话限制](https://hermes-agent.nousresearch.com/docs/developer-guide/gateway-local-sessions) · [Runs API](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server) · [自动复盘的运行条件](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/background_review.py#L228-L240)

### 3. 让新经验自动影响 Codex

| 经验类型 | Hermes 持有的数据 | Codex 自动使用的方式 |
| --- | --- | --- |
| 项目事实、工作约定和用户偏好 | 专用 profile 的 `MEMORY.md` 和 `USER.md`。按原生策略新增和更新。 | `SessionStart` 读已生效且匹配作用域的短快照，`UserPromptSubmit` 可取与当前问题有关的补充。同一版本避免反复注入，原生内存库与 Codex 自有 memories 各自维护。[Hermes 记忆](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) · [Codex Hooks](https://learn.chatgpt.com/docs/hooks) |
| 工具使用方法和程序性技能 | Hermes 原生 `skill_manage` 管理的完整技能目录、使用统计和修改账本。 | 自动发布为 Codex 能发现的项目技能。按名称、内容哈希和作用域同步 `SKILL.md` 及所需引用资源，成功后原子切换版本。新聊天按任务加载技能。反馈区分“目录中出现”“实际读取”“实际采用及结果”，供维护使用。[Hermes Skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills) · [OpenAI Docs · 项目技能发现](https://learn.chatgpt.com/docs/build-skills) |
| 技能整理、归档与恢复 | Curator 根据时间、空闲程度和使用情况维护库，默认检查间隔 7 天、最短空闲 2 小时。模型合并默认关闭。 | 让服务该 profile 的 gateway 负责原生维护，或由独立 worker 定时调用 `maybe_run_curator`，沿用时间和空闲条件。桥回传使用情况，并同步归档或恢复后的目录。本机模拟恢复时同一技能的分类路径改变，名称和内容哈希能正确识别它。技能治理用原生账本和恢复记录。[Curator](https://hermes-agent.nousresearch.com/docs/user-guide/features/curator) |
| 跨日归纳或离线优化 | 可选社区插件 Dreaming，以及 Nous 独立的 DSPy + GEPA 演化项目。后者当前实现了 `SKILL.md` 优化。 | 已配置的可选插件按其事实源和调度运行。积累足够任务样本后，可把技能版本放进基线与留出案例对比，继续通过同一通道发布。核心记忆与技能自动学习可独立运行。[Dreaming 插件](https://hermes-agent.nousresearch.com/docs/plugins/hermes-dreaming) · [Nous Self-Evolution 实现阶段](https://github.com/NousResearch/hermes-agent-self-evolution) |

**自动写入的准确含义。** 实测普通新增记忆、用户画像、创建技能和先读后改的技能补丁可以由后台完成。针对已有记忆的删除性操作如 `patch/remove`，Hermes 可能暂存或拒绝。后台未先读取技能就尝试修改同样可能被拒绝。桥应同步已实际生效的版本，保留原生待审项。这些限制与“是否要用户手动触发总结”是两个独立开关。[记忆原生保护](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/tools/memory_tool.py#L179-L220) · [技能原生保护](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/tools/skill_manager_tool.py)

## 社区已有实现能复用多少

| 实现 | 可复用的设计 | 距本需求还差什么 |
| --- | --- | --- |
| **New-dev0/hermes-codex-learning** | 与需求最接近。已提供 Codex 生命周期 hooks、本地事件、会话摘要和 Hermes 学习收件箱。 | 检查固定提交 `fa5aedc`，`Stop` 脚本将摘要写到磁盘，审阅脚本生成候选文件。还需原生后台复盘、落库回执和新 Codex 聊天自动加载。可以借鉴采集格式。[仓库](https://github.com/New-dev0/hermes-codex-learning) · [Stop 源码](https://github.com/New-dev0/hermes-codex-learning/blob/fa5aedccd926331788d709f14fbfe30248cff46e/plugins/hermes-codex-learning/hooks/stop_export_summary.py) · [审阅脚本](https://github.com/New-dev0/hermes-codex-learning/blob/fa5aedccd926331788d709f14fbfe30248cff46e/plugins/hermes-codex-learning/scripts/review_codex_learning.py) |
| **Plastic Labs codex-honcho / Hindsight Codex** | 前者用启动 hook 注入记忆并在回合结束时写回新增记录。后者的 Codex 接入展示了自动检索、保留历史和排除先前注入内容。 | 两者服务于各自的记忆后端。可借鉴作用域、游标和防止反馈循环的处理，技能生成及 Curator 仍由 Hermes 实现。[codex-honcho](https://github.com/plastic-labs/codex-honcho/tree/c17cbb2b4c7cf12172213345ae4c0d2a093d707b) · [Hindsight Codex](https://github.com/vectorize-io/hindsight/blob/6301f8ce9d68477316caa905d60f207c906752e3/hindsight-integrations/codex/README.md) |
| **本机 Hermes MCP、历史导入和 `coding-dispatch`** | 当前 App 聊天可使用 6 个 Hermes MCP 工具。`coding-dispatch` 已有从 Hermes 派发到 Codex 的工作流。选中的旧 Codex rollout 也能导入 Hermes。 | 工具接口缺少活动代理的学习生命周期。隔离实验中 CLI 两次导入同一文件生成两条 Hermes 会话，并省略原始工具输出，适合旧材料补录。自动主链需要按源事件去重并保留验证结果。[MCP 暴露范围](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/transports/hermes_tools_mcp_server.py#L52-L69) · [历史导入](https://hermes-agent.nousresearch.com/docs/user-guide/sessions) · [本机派发技能](/Users/cy/.hermes/skills/autonomous-ai-agents/coding-dispatch/SKILL.md) |

## 从试验到可日用的验收

1. **先验证真实桌面入口。** 在一个选定项目安装插件并完成一次 hook 信任。用户只提普通任务。检查原聊天仍由 Codex 执行，后台能按正常阈值或短任务策略自动学习。新开聊天后，用一条不含原规则的任务验证记忆、技能发现、实际读取和最终行为，同时测一个相似但不适用的反例。
2. **再验证会话与恢复。** 同一回合重复送达、聊天分叉、取消、压缩、App 退出、worker 重启各做一次。核对同项目作用域和跨项目隔离，确认重复事件不会新增复盘或重复写入，结果未知时可恢复，无可学内容时也能收尾。Codex 自己的后台 hook 依附会话，用它完成持久入队即可。独立进程承接后续模型工作。[OpenAI Docs · 后台 hook 生命周期](https://learn.chatgpt.com/docs/hooks)
3. **最后评估学习质量。** 用真实项目中的一次用户纠正、一次带回归测试的修复、一次偶然失败和一个反例，对照学习开启前后新聊天的实际表现。记录是否正确复用、是否把一次性行为误当长期偏好、复盘消耗和对前台的延迟。随后扩大到更多项目或启用选定的外部 provider、离线技能优化。

本轮交付是源码研究和隔离验证。日用 Codex App、现有 Hermes profiles 及启动项未安装这套桥接。实际 GUI 安装与生产模型验证是下一阶段。
