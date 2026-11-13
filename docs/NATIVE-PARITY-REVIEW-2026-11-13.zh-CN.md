# Codex–Hermes 与原生 Hermes v0.23.1 机制对照

研究日期为 2026-11-13。依据固定版本源码、运行快照和隔离复现，比较原生机制与 Codex 接入。下文记录优化前的基线。

同日的候选构建 `a066b78a1735` 已实现召回和反馈修正、按项目预览记忆及发布、执行主机依赖检查和 macOS 脚本隔离。新构建通过隔离跨聊天反馈验证，Curator 合并、旧名转发与回滚也用真实模型和隔离技能库验证。最新操作入口见[本地运行与验证](./LOCAL-OPERATIONS.zh-CN.md)和[反馈机制](./AUTOMATIC-FEEDBACK-LEARNING.zh-CN.md)。

## 结论

插件确实复用了 Hermes 原生的后台复盘、Memory／Skills 写入工具、审批和 Curator；它的运行环境却是“Codex 执行任务，独立 Hermes 进程观察证据并学习”。原生 Hermes 则由同一个实时 agent 执行、读取技能、当场修改技能，并在回合后派生复盘。两者可以共用学习引擎，但实时上下文、触发计数、缓存、技能采用证据和显式学习语义并不完全相同。[原生实时技能指导][n_live]、[观察者构造与复盘][p_runtime]

当前最值得优先改的是 **让相关经验可靠进入下一次任务、解除应用验证与优先调度开关的耦合、暴露共享记忆池的容量约束**。同时，需要把“代码已支持”和“本机已启用”分开报告。不能因升级到了 0.10.2 就声称语义证据检查、共享发布检查、执行验证、语义整理全部生效。[召回摘要][b_knowledge]、[hook 字节预算][b_capture]、[事实标注开关][b_action_capture]、[能力开关默认值][b_config]

## 基线与证据口径

- 插件研究基线为提交 `2046b777b4320aca6a4eb0da0fe78f480aae9b9e`。
- 官方源码基线为标签 `v0.23.1` 对应的干净 checkout。下文官方源码链接固定到 `3b421578588243f920886e6fe79a6faa87ea026e`。
- 运行使用通过 v0.23.1 manifest 核对的补丁源码。Learning API 和维护适配由补丁提供。[补丁清单][b_manifest]
- 官方网页只补证概念，不覆盖固定版本的源码结论。官网说明了会话起始记忆快照、按需技能加载、Curator 到期与空闲门槛。[Persistent Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)、[Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)、[Curator](https://hermes-agent.nousresearch.com/docs/user-guide/features/curator)
- 本机状态取自北京时间 2026-11-13 00:28–00:38 的只读采样。当时运行插件为 0.10.2、构建摘要前缀 `a174545c`。`capture_scope=global`、`data_policy={}`、`proactive=false`、`checked_publication=false`、`quality_checks=false`、`retention_enabled=false`。原生 `curator.enabled=true`、`consolidate=false`、`interval_hours=168`、`min_idle_hours=2`。仓库与安装包的学习核心 Python 模块一致，差异集中在通知安装模块。

## 能力、配置与实际差异

| 方面 | 原生 v0.23.1 | 插件机制／本机状态 | 判断 |
| --- | --- | --- | --- |
| 实时学习 | 实时 agent 能 `skill_view`、`skill_manage`，发现错误可在任务结束前修订 | Codex MCP 提供上下文、技能读取、反馈和显式学习请求；写入主要交给异步 Hermes owner | 环境分工不同；缺少实时直接修订接口不等于没有学习 |
| 自动复盘 | memory 计用户回合；skill 计执行循环；同会话派生 fork | 终态事件投影、唯一调用 ID 计数、持久化 intent；另有空闲 flush | 阈值复用，计数语义和调度时机不完全相同 |
| 模型上下文 | 原生实时上下文的副本；同模型尽量保持缓存前缀 | 重建外部执行证据并限制到最多 64 KiB，独立观察 agent | 不能声称保留完整 Codex 上下文或直接继承其缓存 |
| Memory／USER | 同一 profile 的文件、冻结会话快照；后台改删通常待审 | 当前 legacy 使用原生工具；召回按来源项目过滤；启用语义准入后只允许项目 Memory | 存储容量、召回范围和写入范围是三件事 |
| 显式 refine | 用户 `/refine` 标记 attended，绕过无人值守改删限制 | `user_request` 与 `attended` 分离；默认 `memory_writes=add`，请求 `revise` 才 attended | 有意收窄授权；应让用户看得懂差别 |
| 显式 learn | 实时正常回合采集资料并写技能，默认不交给 Curator | 分批材料进入同一后台 review fork，新建技能仍具有后台创建来源 | 确定的维护语义差异，需要明确产品决定 |
| 技能可用／使用 | readiness 检查当前 Hermes 环境；正文加载即计 use | 快照交付与显式应用证据分开；当前应用证据为空 | 交付次数和原生 use 都不是收益证明 |
| Curator | 到期、空闲、管理资格、归档／恢复；语义合并默认关闭 | 已接通原生同步维护与来源／别名同步；本机合并关闭 | 主要是部署选择，不是缺少 Curator 代码 |
| 共享与验证 | 原生工具写入校验、锁和来源保护，不提供通用任务成效证明 | 额外发布／内容／沙箱验证已实现，本机关闭；执行验证依赖 Linux `bwrap` | 不能在 Mac 上只打开开关就宣称执行校验可用 |

表中各项的拥有者和边界在下文列明。

## 1. 实时 agent 与后台 fork 的职责

原生提示词要求实时 agent 在匹配时加载技能，并在实际任务发现遗漏、错误命令或新陷阱时修订；这是前台即时改进通道。后台 review 是另一个通道：对已有消息副本运行，禁用递归 nudge，保留受限的 memory／skills 工具与只读文件工具，默认不允许任意执行任务或直接写普通文件。[实时指导][n_live]、[后台工具白名单][n_whitelist]、[fork 构造][n_fork]

插件的 `NativeLearningRuntime.agent()` 创建 `platform=external-observation` 的 agent，设置 `skip_context_files=True`，从学习 profile 解析 provider、model 和 reasoning 配置；`project()` 把已执行的外部证据投影为会话，`review()` 才运行原生 fork。Codex 侧 MCP 没有一个可任意调用的原生 `skill_manage`；它提供 `context`、`skill_load`、`record_application` 和排队的学习入口。[观察者运行时][p_runtime]、[Codex MCP 工具面][b_mcp]、[显式工具契约][b_learning_tools]

实际影响：原生实时 agent 可在看到失败原因的当下把修正写回所用技能；桥接模式依赖终态采集、后台产物和下一轮召回。要改善这一点，首先应修复召回和反馈链，而不是复制一个第二套技能写入器。可以继续使用现有显式请求，对用户要求立即提炼／纠正的回合提供可观察的完成状态；自动任务仍由原生后台学习决定是否值得写入。

## 2. 触发计数并非完全平价

原生 memory nudge 每个用户回合增加一次计数，默认阈值 10；普通 agent 的 skill nudge 在模型执行循环增加计数，在回合结束时检查，默认也是 10。原生 `codex_app_server` 路径使用 `turn.tool_iterations`。因此，“每 10 个工具调用”不是所有原生执行路径统一的定义。[memory 计数][n_memory_tick]、[skill iteration][n_skill_tick]、[回合终结][n_turn_finish]、[原生 Codex 路径][n_codex_finish]、[默认阈值][n_nudge_config]

桥接层没有宿主模型循环的完整事实。补丁按去重的宿主工具调用 ID 累计终态工具项，完成、失败、中断的工具项均参与；同一 turn 的补充 observation 不重复增加用户回合计数。达到阈值且有完成的 assistant 结果才生成 `native_threshold` intent。[observation 准入与计数][p_observe]、[工具项触发适配][p_tick]

本次采样另核对到：根 profile 显式 `skills.creation_nudge_interval=15`，学习 profile 没有该字段，使用默认 10；两份 profile 的模型配置均为 `openai-codex/gpt-6.1-sol`、`codex_responses`、`max`。这是配置对照，不证明某个当前 App bot 正在使用根 profile；也不能把“15 次原生迭代”和“10 个已观测工具项”直接作频率倍数比较。

此外，插件 `idle_reviews()` 不依赖 `proactive=true`。`proactive` 只控制高价值信号优先队列；普通已完成聊天仍在空闲后请求 `maintenance(idle_flush=True)`。配置未覆盖时使用 120 秒空闲和 600 秒检查间隔。补丁 idle flush 在存在未复盘 revision、没有排队／运行／不确定 intent 且没有已尝试覆盖时生成双范围 review；它本身不运行 Curator。[空闲复盘][b_idle]、[时间默认值][b_config]、[idle flush owner][p_maintenance]、[优先级队列][b_scheduling]

这是一种合理的外部执行适配，但应显示 `counter_basis=observed_tool_items` 等事实，并把阈值复盘、空闲复盘、优先反馈和显式请求的延迟／次数分开统计。验收至少包含：同一调用重放不重复计数；并行工具项与单次模型 iteration 的差异被记录；中断回合不被标成成功执行；新 revision 不会被旧 review 错误覆盖。

## 3. 上下文保真与缓存

原生同模型 fork 尽量复制实时 agent 的 system prompt、tools、reasoning 配置和缓存 scope；异模型 fork 则保留最近约 24 条消息并压缩更早的消息。后台 review 默认最多 16 次 iteration，并有累计输入 token 预算。这些本就是原生约束，不是插件造成的遗漏。[原生缓存构造][n_fork]、[review 预算][n_review_budget]、[异模型摘要][n_digest]

插件额外组织目标、追加要求、结论和工具证据，去掉媒体正文、压缩长工具结果，最多保留 64 KiB；更低的原生累计预算还会进一步收紧。异模型路径保留来源明确的对话主线，同时记录丢弃的工具组和可引用范围。这比盲目重放外部 transcript 更可控，但仍不是完整上下文。[复盘输入限额与媒体处理][b_review_input_start]、[预算与适配安装][b_review_input_install]

缓存方面，观察 agent 的系统提示和 Hermes 工具表并不是 Codex Desktop 已发送的请求前缀；不能因 provider／model 同名，就宣称借用了 Codex 主任务缓存。可以测量同一学习 handle 的多次复盘之间是否有缓存收益，但必须看实际 provider 的 cache-read、输入 token 和费用回执。[独立观察 agent][p_runtime]、[原生缓存要求][n_fork]

还要分清两个分支：当前 `data_policy={}` 不安装 `SemanticGuard`；启用非空策略后，`restrict()` 会缩小工具表、关闭外部 memory manager／USER，并替换缓存 system prompt，复盘使用插件的项目与证据提示词。仓库文档中“原生复盘提示词保持原样”的表述应限定到相应分支，不能用于描述所有策略模式。[策略开关与提示替换][b_semantic_install]、[原有机制说明][b_native_doc]

## 4. Memory 是共享小池，项目过滤没有分库

原生 `MemoryStore` 只按 `target` 选择学习 home 下的 `MEMORY.md` 或 `USER.md`；其默认上限分别为 2,200 和 1,375 字符，追加前检查整份文件。各观察 handle 的 `cwd` 不改变该文件位置。原生提示词也明确把 Memory 留给跨任务长期事实，把流程和任务偏好优先放入技能。[文件与容量][n_memory_store]、[整池追加校验][n_memory_add]、[原生记忆分工][n_memory_guidance]

插件在同一学习 home 服务多个项目，`knowledge.context_view()` 根据每条记忆的来源 project 筛选返回内容，各项目仍共享同一容量。本次采样的一份 MEMORY 去除首尾空白后有 1,824 字符、6 条记忆，来源涉及 3 个项目。启用语义准入后的 `ScopedMemory` 也仍把 `add()` 和 `_usage()` 委托给原生同一存储，因而模型只看见项目 A 的条目时，B／C 的占用仍会影响追加操作。[项目召回][b_knowledge]、[ScopedMemory 委托][b_scoped_memory]

建议先增加全池利用率、项目占用、未归属条目、待审压缩数量和最近容量拒绝原因。若实际持续不足，再设计项目存储与用户级记忆的分层：例如每项目原生 profile／独立原生 store，加单独明确授权的用户偏好层。此项需要数据迁移、来源保留、跨项目通用技能共享和恢复方案；单纯调大字符上限只缓解容量，不能解决范围和归属。

USER.md 的边界必须按运行模式说明：

- 当前 legacy：语义限制器未安装，原生 `user_profile_enabled` 可让 fork 使用 USER 工具；不能把目前没有 USER 文件解读为此能力被代码禁止。
- 非空数据策略：`ScopedMemory` 和 `restrict()` 明确关闭 USER，工具 guard 拒绝 `target=user`，稳定偏好也保留在允许的项目范围。
- Codex 召回：Memory 和 USER 条目都需要匹配的项目来源；没有一个通用用户画像回注通道。原生 provider recall 在全局 context view 中被明确抑制，避免把未分项目的 provider 结果混入。

依据：[安装条件][b_semantic_install]、[项目 Memory guard][b_semantic_guard]、[召回筛选与 provider 抑制][b_knowledge]。如果希望跨项目保持同一种工作偏好，需要单独定义这一产品能力，不能靠放开全部 USER／provider 正文来代替范围设计。

## 5. 待审与显式 `/refine`、`/learn`

原生无人值守 review 允许 add；patch／remove 会绑定匹配条目并进入 pending。这是上游保护，不能列作插件“不会纠正记忆”的缺陷。原生 `/refine` 总是传 `explicit=True`，fork 仍是 `background_review` 来源，但标记 attended，从而不受无人值守改删门槛限制；其他写入审批政策仍然适用。[原生待审 gate][n_pending]、[/refine 派发][n_refine]、[attended 与 origin][n_origin]、[fork attended][n_review_run]

插件把两个决定分开：`user_request` 只表示用户要求现在复盘，可绕过自动 review disabled／defer；是否允许现有记忆修订还取决于 `memory_writes=revise`。默认 `add` 因而仍可能产出待审提案。项目 pending 入口能够展示具体差异和过期状态；它只处理项目 Memory，不是原生 USER 审批界面的全量替代。[显式请求校验][b_learning_request]、[attended 传递][b_learning_dispatch]、[原生 runtime 接收][p_runtime]、[项目 pending][b_approvals]

原生 `/learn` 是正常的实时回合：可用已有工具读取用户指定路径、网页和对话，再按技能标准分批提炼。插件则由 Codex 读取／选择材料，通过最多 64 个、每个最多 24 KiB 的 chunk 提交，逐批复盘，最后校对 references 和 SKILL.md 索引。这个结构支持恢复和来源记录，但“索引齐全”只证明结构，不证明语义正确或方法执行有效。[原生 learn][n_learn]、[插件材料契约][b_learning_tools]、[分批处理及结构校验][b_learning_dispatch]

**一个需要明确处理的平价缺口：显式资料学习的 Curator 归属。** 原生实时 `/learn` 创建技能标为 `created_by=learn`，默认不自动交给 Curator；插件 `mode=learn` 仍通过后台 review fork 创建，原生 stamp 由 `is_background_review()` 决定，所以默认为 `created_by=agent`，进入自主维护范围。`memory_writes=revise` 的 attended 标记不会改变这个来源。[创建 stamp owner][n_skill_stamp]、[created_by 语义][n_created]、[插件学习派发][b_learning_dispatch]

建议将“用户明确教给系统的资料技能”和“自动提炼技能”的维护意图独立记录，再决定是否沿用原生 `learn`／显式 adopt 语义。不要仅为了改 stamp 把整个 review 改成 foreground，因为那会同时改变其它原生保护。验收要实际比较两个 learn 入口创建后的管理资格、自动 Curator 选择及显式 adopt／pin 行为。

## 6. readiness、use、validation 是不同事实

原生 `skill_view` 会检查平台、环境变量／凭据文件并处理声明的依赖；它不等于测试技能业务正确性。原生 wrapper 在成功加载正文后增加 view 与 use，use 的含义包括“加载到提示中”；Curator 使用这些活动时间，不是用它们判断任务成功率。[原生 readiness][n_readiness]、[依赖处理][n_skill_load]、[原生 view／use][n_use]

插件当前用先前准备的不可变包响应 `skill_load`，避免读操作直接触发后台模型。它签发交付 token，只有宿主真实 PostToolUse 证据确认正文交付后才回写原生 use；`record_application` 另记应用声明及宿主测试引用。测试通过仍明确保留 `effectiveness=not_inferred`。[快照读取][b_skill_reads]、[交付捕获][b_delivery]、[原生计 use][p_usage]、[应用反馈][b_feedback]

本次采样发现 `application_evidence=0`、`delivery_events=0`，而历史 direct skill_load hooks 和 native deliveries 有记录。它证明目前不能从数据库得到完整“交付→采用→测试”证据，**不能直接证明没有使用，也不能直接把缺口归因于某一种工具 wrapper**。应以现有宿主结构逐段确认捕获、token 校验、drain、原生 use、应用反馈。真实验收应绑定同一技能版本完成一次加载与实际任务，随后核对这些独立状态，不能以后台读取计数代替。

**已有隔离复现的 P1 缺口：测试事实标记依赖 `proactive`。** `capture()` 仅在 `proactive=true` 时执行 `scheduling.annotate()`；normalizer 从其 `bridge_action` 取得 `action`；反馈核对又只接受 `action=test`。因此关闭优先调度，会同时让正常捕获的测试失去类型信息。本次采样看到 3,139 个保留工具项的 action 均为空，并用已安装包在临时 home 走真实 hook→merge_event→feedback.record：合成已交付回执与退出码 0 的 `python3 -m unittest` 在 proactive=false 时保持 pending，true 时为 host_test_passed。没有手工补 bridge_action，没有真实模型或真实测试执行，生产文件未写入。[采集条件][b_action_capture]、[归一化字段][b_action_normalize]、[反馈核对][b_feedback]

即使 proactive=true，分类器仍只认简单 pytest／unittest；相同隔离复现中 `npm test`、`python3 scripts/check_release.py` 被分为 execute，验证保持 pending。建议把事实性动作归一化独立于调度优先级，按实际支持的验证入口扩展明确分类，保留命令复杂性与来源检查，并制定旧数据重算策略。验收应走 hook 正常入口，覆盖开关两种状态、常见验证入口、失败／未知终态和重复事件；不能把任意成功 shell 命令升级为有效测试，也不能把测试通过升级为业务成效。[分类器][b_action_workflow]

readiness 还存在环境和时间差：`learning_artifacts.skill()` 在学习进程环境调用原生读取，仅复制部分 readiness 字段；`snapshot()` 把内容存在标成 `available`；Codex 读取缓存并不重查当前执行主机。因此应将内容可读、学习主机依赖检查、执行主机准备情况分列，带检查时间／主机，并避免把旧 `available` 当作跨主机就绪证明。[产物 readiness][p_artifacts]、[缓存复用][b_cache_artifacts]、[读取路径][b_skill_reads]

插件额外的 quality verifier 先做内容结构检查；含 Python／shell 资源时要求独立 suite 和 `bwrap` 隔离环境。本机 Mac 未具备该验证后端，简单启用 `quality_checks` 只会得到 `verified_sandbox_unavailable` 等 pending，不能宣称完成独立执行验证。应选择受支持的验证运行环境，或补充经过验证的 Mac 后端，并按 artifact hash 绑定结果。[质量验证 owner][b_quality]

## 7. Curator 已接通，语义整理仍取决于配置

原生 Curator 的 `enabled` 默认开启，但 `consolidate` 默认关闭；开启前者只保证到期后可做确定性的 stale／archive 维护。周期语义合并需要后者。第一次观察会先设置时间基线、延后一整个周期，因此没有近期合并记录也可能是门槛尚未满足。[原生配置与到期][n_curator_config]、[确定性维护][n_curator_transition]、[整理开关][n_curator_run]

插件 maintenance 沿用原生到期／空闲／占用 claim，然后同步执行原生 Curator，避免唯一 owner 在整理未完成时继续发布或退出。daemon 只在没有活动宿主回合时进入维护，并取宿主与原生 observation 两方的空闲约束。`idle_flush` 与该库级维护分开。[插件维护调度][b_maintenance]、[原生维护适配][p_native_maintenance]、[宿主 idle flush 分离][p_maintenance]、[worker 调度][b_worker_maintenance]

插件已处理账本分页、来源合并、`absorbed_into`、旧名转发、恢复原归属，以及新发布失败时不立即丢失旧可读版本；不是从模型总结推断所有合并。缺失账本游标会明确停在 `history_gap`，原生整库 rollback 则要求版本与已保存归档信息匹配才恢复归属。[生命周期同步][b_lifecycle]、[恢复处理][b_restore]、[原生账本事件适配][p_native_maintenance]

原生归档／恢复是可恢复目录迁移；备份和账本记录有 best-effort 边界，不是跨文件原子事务。这些限制同样存在于原生，不能把它们描述为插件新引入的问题。[原生归档与恢复][n_archive]、[备份及整理流程][n_curator_run]

本机 `consolidate=false` 是“语义整理未启用”，并非“没有实现语义整理”。如后续决定开启，应先在隔离副本验收合并→发布拒绝仍可读旧版→发布成功后旧名转发→原生 restore 恢复来源；另外检查用户 `/learn` 技能的维护资格，再改变生产配置。留存也已有原生快照引用保护与 30 天宽限实现，但本机 `retention_enabled=false` 尚未触发每日流程。[留存 owner][b_retention]、[每日开关][b_worker_maintenance]

## 8. 召回有两处可直接修复的缺口

### 8.1 hook 预算挤掉相关技能

`context_view()` 先截取 Memory 的前 340 字符，再拼相关技能名；`model_context()` 又把完整 hook 限制为 256 UTF-8 字节，其中还包括绝对项目路径和固定提示。本次针对用户问题的只读复算得到 4 项推荐，但固定文字占 156 字节，留给摘要 100 字节，最终一个推荐技能名也未留下。源码足以解释这一现象。[摘要拼接][b_knowledge]、[256 字节裁剪][b_capture]

建议在同样有界的提示中先为 1–3 个最相关技能名保留结构化空间，剩余预算才放短记忆线索；路径可放在结构化上下文或短引用中。还应记录实际送入 hook 的技能名，而非只保存裁剪前的 `recommended_skills`。验收覆盖长路径、中文 UTF-8、长 Memory、无 Memory、多技能名称和重复版本；至少一个已匹配且可用的技能名应能进入模型可见提示。

### 8.2 关联技能只处理一个方向

原生图把 `related_skills` 建成无向边，但补丁 catalog 提供各节点原始声明。插件推荐只检查“候选自己的 related_skills 是否含直接命中技能”，漏掉“直接命中技能声明了候选”的方向。现有 lifecycle 测试只覆盖前一种。[原生关系图][n_graph]、[节点输出][p_artifacts]、[插件推荐][b_knowledge]、[现有用例][b_related_test]

本次仅用纯函数、合成对象验证，未访问服务或模型：`deploy.related_skills=[rollback]`、查询 `deployment` 只返回 deploy；把声明放在 rollback 指向 deploy 时两者都返回。建议在来源／发布过滤后生成双向一跳邻接，再排序去重；仍限制最多五项，不能用关联关系绕过项目或企业范围。验收同时覆盖两个方向、循环、失效目标和跨范围边。

另外，启用语义准入后的“可修订技能”筛选是独立逻辑：按名称／描述概念交集且至少两个概念、最多十项，并不使用原生关联边。这是保守的写入范围选择，不应与召回目录推荐混称同一个检索系统。可用真实任务检查是否漏掉正确旧技能、造成近重复新建，再决定是否扩展。[写入相关性 owner][b_relevance]

## 建议实施顺序与验收

| 优先级 | 工作 | 类型 | 可判定的验收结果 |
| --- | --- | --- | --- |
| P1 | 修复 hook 的技能名预算，记录裁剪后的真实 offer | 代码 | 长路径＋长中文 Memory 下仍保留相关技能入口；不扩大到全库正文 |
| P1 | 解耦测试事实标记与 proactive，并核清交付／应用反馈 | 代码＋真实任务 | 开关关闭时正常测试仍能识别；同一 version 的读取、正文交付、采用、测试状态可串起来；成功退出不自动等于有效测试或学习收益 |
| P1 | 暴露共享 Memory 的容量与待审压力 | 代码；后续才是存储设计 | 三个项目共同填充接近上限时，能准确说明谁占空间、为何拒绝、该在哪里审阅；项目 A 不收到 B 的正文 |
| P1 | 给本机生成“可用能力／生效策略／未验证环节”状态说明 | 配置审计＋文档 | 空 data_policy 不再被描述成已运行证据 guard；合并／质量／留存关闭分别显示；checked_publication 迁移前清楚预览旧库可见性变化 |
| P2 | 明确显式 learn 的 Curator opt-in 语义 | 产品决定＋代码 | 用户资料 learn 与自动学习的管理资格符合既定选择；保留原生后台写入保护 |
| P2 | 双向 related_skills 与执行主机 readiness | 代码 | 双向关系在范围内召回；依赖变化／跨主机快照不会虚报执行就绪 |
| P2 | 按原生 owner 激活需要的 Curator、留存、发布能力 | 配置＋隔离恢复验收 | 到期／空闲才执行；来源、别名、历史版本、restore、保留引用全部可验证；Mac 执行验证后端另行解决 |
| P2 | 增加计数语义、裁剪覆盖、复盘成本的可观测性 | 代码＋provider 实测 | 区分 observed tool items 与 iterations；显示实际输入覆盖、模型路由、cache-read 与费用，不能只报成功次数 |

以上八项是待评估的改进，不构成本次配置变更，也不建议立即把所有开关打开。尤其 checked_publication 会改变旧库跨项目可见性，应先预览和处理来源／发布状态。已有原生能力应继续由原生 owner 执行；应优先补接入层的可见性、来源和交付，而非重复实现另一个 memory／skills／Curator 系统。

修复并验收这些环节后，再用真实工作观察任务完成时间、重复错误和人工纠正次数，对照相似任务并记录干扰因素。技能数、读取次数和测试通过率本身都不是因果收益证明。

## 本次验证的边界

已核对固定官方 commit、插件代码、已安装学习核心及补丁源码，并读取官方文档。验证包含实际缓存的 hook 预算复算、关联推荐的无状态合成输入，以及临时 home 中四组 hook→归一化→应用验证路径。四组输入中的成功退出码属于合成数据，验证范围是接入层的识别和状态转换。本次真实模型学习质量、生产 Curator 合并效果与 Desktop 跨聊天业务收益仍待相应任务验证。报告中的运行开关和计数取自本机采样，实际采用情况只可按已有应用证据判断。

官方链接固定到 v0.23.1 提交。插件链接指向研究时的代码，补丁链接指向对应的模块改动。

[n_live]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/prompt_builder.py#L1339-L1357
[n_whitelist]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/background_review.py#L1116-L1156
[n_fork]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/background_review.py#L935-L1061
[n_memory_tick]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/turn_context.py#L819-L828
[n_skill_tick]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/turn_iteration_prep.py#L165-L167
[n_turn_finish]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/turn_finalizer.py#L862-L875
[n_codex_finish]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/codex_runtime.py#L700-L722
[n_nudge_config]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/agent_init.py#L1155-L1238
[n_review_budget]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/background_review.py#L170-L182
[n_digest]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/background_review.py#L332-L362
[n_memory_store]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/memory_tool_store.py#L177-L255
[n_memory_add]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/memory_tool_store.py#L300-L413
[n_memory_guidance]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/prompt_builder.py#L196-L231
[n_pending]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/memory_tool.py#L179-L218
[n_refine]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/hermes_cli/cli_commands_mixin.py#L1661-L1680
[n_origin]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/skill_provenance.py#L25-L44
[n_review_run]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/background_review.py#L1189-L1235
[n_learn]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/learn_prompt.py#L136-L185
[n_skill_stamp]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/skill_manager_tool.py#L763-L779
[n_created]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/skill_usage.py#L522-L534
[n_readiness]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/skills_tool.py#L470-L513
[n_skill_load]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/skills_tool.py#L641-L707
[n_use]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/skills_tool.py#L762-L780
[n_curator_config]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/curator.py#L106-L179
[n_curator_transition]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/curator.py#L209-L263
[n_curator_run]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/curator.py#L931-L1003
[n_archive]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/skill_usage.py#L610-L696
[n_graph]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/learning_graph.py#L116-L129
[b_manifest]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/patches/v0.23.1/manifest.json#L2
[b_config]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/config.py#L141
[b_mcp]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/mcp.py#L9
[b_learning_tools]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/learning_tools.py#L12
[b_idle]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/daemon.py#L475
[b_scheduling]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/scheduling.py#L39
[b_review_input_start]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/review_input.py#L10
[b_review_input_install]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/review_input.py#L249
[b_semantic_install]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/semantic_review.py#L384
[b_semantic_guard]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/semantic_review.py#L145
[b_scoped_memory]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/semantic_review.py#L71
[b_native_doc]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/docs/NATIVE-LIFECYCLE.zh-CN.md#L3
[b_knowledge]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/knowledge.py#L82
[b_learning_request]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/learning_requests.py#L104
[b_learning_dispatch]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/learning_dispatch.py#L12
[b_approvals]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/approvals.py#L24
[b_skill_reads]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/skill_reads.py#L12
[b_delivery]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/delivery.py#L9
[b_feedback]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/feedback.py#L10
[b_cache_artifacts]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/daemon.py#L572
[b_quality]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/quality.py#L63
[b_maintenance]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/lifecycle.py#L155
[b_worker_maintenance]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/daemon.py#L817
[b_lifecycle]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/lifecycle.py#L45
[b_restore]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/lifecycle.py#L112
[b_retention]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/retention.py#L100
[b_capture]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/capture.py#L15
[b_action_capture]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/capture.py#L92
[b_action_normalize]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/normalize.py#L131
[b_action_workflow]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/workflow.py#L17
[b_related_test]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/tests/test_lifecycle.py#L96
[b_relevance]: https://github.com/cYz26/codex-hermes-plugin/blob/2046b777b4320aca6a4eb0da0fe78f480aae9b9e/src/hermes_codex_learning/review_relevance.py#L75
[p_runtime]: ../patches/v0.23.0/native-learning-api-v1.patch#L474
[p_observe]: ../patches/v0.23.0/native-learning-api-v1.patch#L648
[p_tick]: ../patches/v0.23.0/native-learning-api-v1.patch#L385
[p_maintenance]: ../patches/v0.23.0/native-learning-api-v1.patch#L648
[p_native_maintenance]: ../patches/v0.23.0/native-learning-api-v1.patch#L238
[p_artifacts]: ../patches/v0.23.0/native-learning-api-v1.patch#L117
[p_usage]: ../patches/v0.23.0/native-learning-api-v1.patch#L648
