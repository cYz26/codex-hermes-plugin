# Codex × Hermes：重新比较最优路线

2026-11-03。**最新约束：Codex App 保持真实任务执行与主体交互；Codex 核心不改；Hermes 侧暂不限制改动面，以实际最佳方案为目标。此前“双方核心均不改”的约束已经放宽。** 本文仅研究，没有安装、修改生产配置、读取日用会话/凭据或调用模型。

Hermes 本机基线：`v0.22.5` / `bf0203a794a07aac3ff764972ab9087baa4e4f4e`。社区候选通过作者仓库、README 与关键代码核对，不以名称、目录收录或 star 数作为原生能力证明。

> 后续收敛：用户要求减少 Hermes 升级维护面，现建议把原生外部轨迹服务拆为一个通用 Learning API 与外围桥接插件。核心保留学习生命周期和最小事实收据，完整 job 队列、调度、传输和发布移到插件。下文路线比较仍有效，具体归属以[当前主方案](/Users/cy/Dev/agents-dev/hermes-plugin1102/HERMES-CODEX-PLAN.md)和[扩展合同审查](/Users/cy/Dev/agents-dev/hermes-plugin1102/HERMES-CODEX-EXTENSION-REVIEW.md)为准。

## 推荐发生了变化

**长期最佳：给 Hermes 增加正式的外部轨迹学习服务，由 Codex 外围插件发送已完成的事件；Hermes 自己负责规范化存储、原生学习策略、复盘 job、记忆/技能和采用反馈。** 公开普通学习会话仍是低改动试点/回退路线，但不再因为“不能改 Hermes”而被当作能力上限。

依据是本机已经存在两段适合提取的代码：`CodexEventProjector` 将完成的 Codex item 投影为 user/assistant/tool 消息，并标注真实 tool item 计数；`_persist_projected_messages` 与 `_finish_codex_turn` 已接上持久化、计数、MemoryProvider 和后台复盘。目标应是把这些语义提为受支持的共享服务，让 Hermes 自有 Codex runtime 和外部 Codex observer 共用，而非由插件复制一份 finalizer、造一个假用户回合或直接写 DB。[现有 projector](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/transports/codex_event_projector.py#L1)、[现有持久化与完成流程](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/codex_runtime.py#L666)

这仍是架构建议，不是已存在的接口，也未证明实现后完全等同于 Hermes 自己执行。外部轨迹应明确标注 source，保留 Codex ID/项目/分叉/内容 hash；工具调用是 Codex 已执行的观察事实，不是授权 Hermes 重新运行的命令。

## 现成候选：哪些真的解决了本需求

| 路线 | 本次一手证据 | 判断 |
|---|---|---|
| **Hermes 原生 external-learning service（新增）** | 现有 projector + runtime finish 提供可复用语义，但没有现成外部 admission/job 合同 | 新约束下的长期首选；最能减少假主回合、计数失真和观测缺口；核心工程与维护成本最高。 |
| **公开 gateway 普通学习会话 + evidence 工具 + nudge** | 原生 session/prompt 接口，工具结果入库，原生复盘 | 适合先做端到端试点；多一次学习主回合，阈值针对学习会话而非原始任务；自动 review 仍可能被新 turn 抢占。 |
| **New-dev0/hermes-codex-learning** | 导出 lifecycle events、summary、候选 review 文件及 inbox skill | 可借鉴采集/脱敏/inbox；未实现自动原生闭环。示例 `hermes chat -q` 在本机属于 finite，不能据此宣称默认 nudge 已触发。 |
| **LYAKAKOY/Hermes-Codex-Plugin** | 自有 SQLite/FTS、Codex hooks/MCP、规则提炼与技能草稿；不要求 daemon | 是独立的 Codex 记忆插件；不是 Nous Hermes 学习代理、MemoryProvider、native review 或 Curator 的封装。 |
| **plastic-labs/codex-honcho** | Honcho 官方团队的 hooks+MCP+skill、队列/cursor、自动召回/上传 | 如果目标只是跨会话记忆，是更完整的现成替代；没有 Hermes 原生技能提炼/Curator。可选为共享事实层，不能代替学习服务。 |
| **Hermes codex_app_server runtime** | 官方原生：Hermes 管会话，Codex runtime 执行，事件投影后原生复盘 | 原生对齐度很高，是新服务的实现参照；主会话仍归 Hermes，不满足现有 Codex App chat 原地执行的产品边界。 |
| **官方 mcp-serve / 工具 MCP** | 官方插件暴露会话读写、审批与固定工具 allowlist | 适合作为取回技能/读取能力；暴露工具不等于监听外部 Codex 轨迹并触发完整学习。 |
| **intellectronica/hermes-chatgpt-extension** | 在 Codex 内嵌 Hermes chat，明确外层 Codex conversation 独立 | 能满足“窗口在 Codex”，但执行仍由 Hermes 接管，不能混同于 Codex 原有任务自动学习。 |

以上候选在线核对来源：[New-dev0](https://github.com/New-dev0/hermes-codex-learning)、[LYAKAKOY](https://github.com/LYAKAKOY/Hermes-Codex-Plugin)、[Plastic Labs](https://github.com/plastic-labs/codex-honcho)、[官方 Codex runtime](https://hermes-agent.nousresearch.com/docs/user-guide/features/codex-app-server-runtime)、[官方 MCP Serve](https://github.com/NousResearch/hermes-official-plugins/tree/main/mcp-serve)、[内嵌 Hermes chat](https://github.com/intellectronica/hermes-chatgpt-extension)。这是本次查到的相关候选，不是生态中不存在其他方案的证明。

### 三个社区候选的代码反证

- `New-dev0` 核对提交 `fa5aedccd926331788d709f14fbfe30248cff46e`。`review_codex_learning.py` 是候选分类和 Markdown 输出，包含“不自动写 arbitrary memory”的说明，没有启动 native review。它的“review 文件”不是一次 Hermes 推理成功或技能生效的证据。[脚本](https://github.com/New-dev0/hermes-codex-learning/blob/fa5aedccd926331788d709f14fbfe30248cff46e/plugins/hermes-codex-learning/scripts/review_codex_learning.py#L50)
- `LYAKAKOY` 核对提交 `af556a6cdab780b2dceb15a7354da393f1656240`，包版本 0.10.2。依赖为 SQLite/配置/日志/ORM，技能 domain code 是自己的规则句提取；它的 Hermes 名称不能解释为 Nous Hermes 原生机制。[依赖](https://github.com/LYAKAKOY/Hermes-Codex-Plugin/blob/af556a6cdab780b2dceb15a7354da393f1656240/plugins/hermes-codex-plugin/pyproject.toml#L23)、[规则提取](https://github.com/LYAKAKOY/Hermes-Codex-Plugin/blob/af556a6cdab780b2dceb15a7354da393f1656240/plugins/hermes-codex-plugin/src/hermes_codex_plugin/domain/skills/services.py#L87)
- `codex-honcho` 核对提交 `c17cbb2b4c7cf12172213345ae4c0d2a093d707b`。Stop/PreCompact 先 capture，再 **inline await flush**；代码说明 Codex 会在 hook 返回后杀 detached children，所以不能照 README 的“后台”措辞假定任意 detached worker 能存活。它已有可靠队列/cursor设计，值得借鉴；实际 App 兼容性还应按当前版本实测。[writeback 实现](https://github.com/plastic-labs/codex-honcho/blob/c17cbb2b4c7cf12172213345ae4c0d2a093d707b/src/hooks/writeback.ts#L13)

## 原生外部轨迹服务应该复用什么、不能直接调用什么

建议分成四个明确边界（以下名字是建议协议，不是现有产品）：

1. **接收观察事实。** 拟新增的 `learning.turn.ingest` 接受 schema 版本、source host/project/chat/turn/item ID、parent/fork 信息、完成/失败/中断状态、事件序列与 hash。按真实 source ID 做幂等；保存 raw evidence 和 normalized transcript；先提交 durable receipt，不做模型调用。以原生 session 服务写入专用 source 类型，外部插件不碰 DB。
2. **统一学习完成语义。** 从 `_finish_codex_turn` 和普通 finalizer 中提取“对已提交 turn 更新学习计数、给 provider 同步、决定 review”的组件。传入真实用户回合与 completed tool items；不要把每条 JSON 事件或流式 delta 都当迭代。外部数据没有的状态必须 unknown，不凭猜测补齐。
3. **原生 review job。** 单独持久化待复盘 snapshot/version、trigger、policy version、attempt、状态与成本，使用现有原生 prompt、模型路由、memory/skill tools、background write origin 和 guards。执行状态与 applied/staged/uncertain 结果列表分开，保留失败前的部分写入；确认成功结束且无变更才标记 no_change。与在线用户前台抢占策略分开：后台学习器没有真人 foreground 任务，但仍需接受暂停/取消并恢复未完成工作。
4. **发布与采用反馈。** 对已生效产物生成版本/hash清单，通过 Codex MCP/技能/上下文入口读取；真实加载调用 native skill_view，质量结果再回传为 source evidence。归档/恢复由原生 Curator 管理，插件不复制账本逻辑。

**不能简单把 `_finish_codex_turn` 暴露出去。** 它假设 `run_conversation` 已增加用户/记忆计数，还混有 app-server compaction 与 usage 记账；直接调用会漏计/重计，或把外部 Codex tokens 重复记成 Hermes 推理。它当前的 provider sync 与 review 条件也不完全相同，需要明确 accepted/completed/error 语义再抽取。应保留 source_usage 与 learner_usage 分账，并对外部日志、未完成调用、已注入旧记忆设归因规则。[这些假设所在代码](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/codex_runtime.py#L701)

**不能把 projector 当无损轨迹格式。** 当前只在 `item/completed` 生成记录；fileChange 只保留文件/kind 摘要，未知 item 变 opaque note，用户多模态也不是完整保留。新服务可共用规范化逻辑，但必须额外保存原始 artifacts，并对映射版本、缺失 ID、分叉与重新连接做兼容性测试。若外部只能拿到 hooks 的部分摘要，就应承认证据覆盖率，不填造 app-server 事件。[投影代码](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/transports/codex_event_projector.py#L48)、[fileChange 摘要](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/transports/codex_event_projector.py#L117)

核心工程风险集中在 source transcript 与 live session 的隔离、DB幂等、provider attribution、部分写入后的重试、进程恢复及共享 review 引擎的回归；不在 HTTP 路由本身。应先抽取纯 event projector 与 completed-turn learning contract，保留 Hermes 自有 Codex runtime 回归，再加外部 admission。没有依据给出可信的工期数字，也没有必要先复制整套 agent loop。

## Profile、学习 session 与触发方式

| 选择 | 收益 | 代价与推荐 |
|---|---|---|
| 每项目一个专用 profile | 技能/记忆/Curator 边界自然；便于试点清理与来源审计 | 配置和公共偏好会分散。首版单项目很好；未来按知识/权限域划分，不必永久每个小 repo 都建 profile。 |
| 每 Codex chat 对应学习 session，任务/主题自然结束时轮换 | 原始上下文容易关联；避免项目全史持续膨胀 | 一个 chat 也可能跨数周，需 completed task boundary/预算轮换；source chat ID 仍稳定关联多个学习 segment。推荐默认。 |
| 每项目永久一个学习会话 | 共享上下文、阈值易累计、缓存可能命中 | 跨任务混杂；新 memory 快照不刷新；反复 compaction；未来每次 review 重读大历史。一般不推荐。 |
| 每任务完全孤立学习 session | 成本/来源容易界定，天然新 memory 快照 | 短任务到不了默认阈值，跨任务模式需要检索或周期聚合。新服务可保留项目级真实计数/聚合，而不造长前台聊天。 |

Hermes 官方明确 memory prompt snapshot 在会话开始冻结，建议在完成任务/换主题时开启新 session；长期不切换会提高 compaction 成本，并减少实际记忆召回机会。profile 则是持久知识边界，不应把 profile 与 session 当同一层。[原生 memory session boundary](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/website/docs/user-guide/features/memory.md#L57)、[profile 隔离](https://hermes-agent.nousresearch.com/docs/user-guide/profiles)

触发方式排序：**新服务的原生计数/任务完成学习政策 > 现有普通学习 turn+nudge > 自动 `/refine`。** 新服务默认复用原有真实计数，额外任务结束/用户纠正触发应成为显式 policy，而非假装 nudge threshold 到达。公开会话试点可将 1/1 用于短任务，但要承认改变频率；不要灌空回合或虚构 tool iterations。自动 `/refine` 虽少一个 learning turn，却被标为 explicit/attended，使 memory destructive gate 不同，不能作为默认无人值守 nudge 的同义词。[自动触发](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/turn_finalizer.py#L763)、[refine explicit](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/gateway/session_refine.py#L69)

## 退出、成本与真实反馈

原生即时 review 是 daemon thread；新 turn 会取消它，取消等待有上限。受管本地模型的原生 idle queue 只在内存中，按 session 合并并有限重排，退出会丢 pending 工作；主 admission 的 durable FIFO 并不能保证 review 同步恢复。因此真正产品化应有学习 job journal，区分“观察已接收”“主输入已完成”和“review已完成”。状态观察插件可能改善可见性，但只有观察不能自动提供事务重试与恢复。[取消路径](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/background_review.py#L129)、[idle queue 非持久性](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/review_idle_queue.py#L1)

新 observer 服务省掉一个“为学习再推理”的主回合，但不能直接声称总 token 更低：真实 Hermes 任务后的同模型 review 能使用已预热的 prompt prefix，外部 Codex App 的缓存未必能由 Hermes复用；长轨迹直接 review 可能冷读。不同辅助模型又会使用 digest，降低成本同时损失旧工具细节。验收应记录 source bytes/覆盖率、学习输入输出 tokens、cache hit、产物/复用率与实际改善；避免按“生成了几个技能”奖励膨胀。[同模型/full snapshot 与不同模型/digest](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/background_review.py#L168)、[review 输入预算](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/background_review.py#L173)

真实采用至少区分 discovered、loaded、applied、validated。native skill_view 的 use 代表实际加载，不是执行成功；不能只导出文件就人为加 use，或把所有候选浏览都算采用。采用反馈带 skill version/hash、Codex turn、测试/人工纠正 evidence，作为下一轮学习材料；不直接改技能评分/usage JSON。后台新建技能原生进入 Curator，前台产生的 `created_by: learn` 通过单技能公开 adopt 明确纳管即可。[skill_view 原生反馈](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/tools/skills_tool.py#L737)、[adopt 合同](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/tools/skill_usage.py#L290)

## Codex 自身 memories 的归属

设计上保持一个知识条目有明确权威来源：Hermes 学习产物由专用 profile 管理，Codex 插件按项目/版本只读注入；Codex 自有 memories 继续遵守 Codex 当前产品与用户授权策略，Hermes 不直接改写 `~/.codex/memories`。当前会话明确指令、仓库规则与已确认事实优先于过去的自动推断；跨源冲突应保留来源与时间，不能让两个系统静默互相覆盖。

不要把注入过的 Hermes memory 再当作新的人类证据回灌，也不要让 Codex 与 Hermes 各自独立向同一 Honcho peer 写同一份原文而未做来源/幂等设计。可选择 Honcho 作为共享事实层，但它不自动统一 Hermes技能/Curator与Codex memories的职责。上述是避免循环学习与双写冲突的架构建议，不是声称已验证所有宿主的 memory 实现。

建议推进顺序：先验证目标 Codex App 的实际采集和经验读回，再提取 Hermes 共享学习生命周期、接入正式外部轨迹入口及持久 job，最后做真实任务的学习与采用对照。现有公开会话可用于回退或单独比较学习效果，无需把它产品化作为前置条件。具体以[当前主方案](/Users/cy/Dev/agents-dev/hermes-plugin1102/HERMES-CODEX-PLAN.md)为准。
