# Codex＋Hermes 与原生 Hermes 的共享学习边界

研究日期：2026-11-15。本文保留实施前对官方文档与固定源码的核验。研究期间未读取生产记忆或会话正文，未启动模型、provider、复盘或服务，未修改配置。实际接入方式见[共享学习说明](./SHARED-LEARNING.zh-CN.md)。

## 结论

除了技能，两端可以共享**同一用户明确确认的偏好、同项目的事实与决策、带证据的成功／失败经验、技能的精确版本及采用／验证反馈、归档／替换关系与来源**。应共享这些有范围的成果与事件，保留各自的会话库、执行身份、原生局部记忆及局部复盘。原生已有相当多的读写、复盘、归档和审计能力，但没有一项现成配置可以把两个 Hermes home 变成具备冲突控制、项目隔离和真实效果反馈的统一知识库。[N1]、[N2]、[N5]、[N8]、[N12]、[P1]、[P3]

建议把现有 pilot 学习服务作为**共享成果的唯一写入与整理方**：两端仍运行各自的原生能力；原生侧把候选修改、来源及基线版本提交给 pilot，由现有发布过程决定公共版本；两端读取已发布版本。原生 memory 侧优先用薄的本地 `MemoryProvider` 适配 pilot 的有范围 API；普通插件 hooks 采集工具证据与 skill lifecycle，现有 MCP 提供按需技能及历史读取。`external_dirs` 可用于技能发现，但必须另外落实写入保护。此方案是基于下列源码边界的架构推论，并非上游已交付的双向同步功能。[N3]、[N6]、[N7]、[N10]、[N11]、[P1]、[P2]、[P3]

## 核验基线

| 对象 | 本次核验结果 |
|---|---|
| 官方源码 | 干净检出；tag `v0.23.1`；commit `3b421578588243f920886e6fe79a6faa87ea026e`；提交日期 2026-11-09。下文所有 N 类源码链接固定到此提交。 |
| pilot 的 `bridge.json` 选择的源码 | 本机候选 `v0.23.1-bfca62ab555f`；同一官方 HEAD，有 Learning API 等 staged 补丁。只核对了 source／python 路径字段；未据此断言某个运行中进程已重载全部文件。 |
| 本插件源码 | commit `fe0abd386c9244387d58f9e1e613bbe0b9c0d19d`，读取开始时工作区干净。P 类引用按此时源码行号；后续并行任务的工作区变更不作为本研究基线。 |
| 补丁性质 | `agent/learning_api.py`、`learning_service.py`、`learning_runtime.py`、`learning_artifacts.py`、`learning_maintenance.py` 是本地补丁新增文件，不是该官方 commit 的无补丁 API。 |

为便于复核已读补丁内容，当前 bridge-selected 源码文件 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `agent/learning_api.py` | `0ab0b488a268e14b83385c8e4c5af410337db8034bb31ef1f66aeb9c8699ccf8` |
| `agent/learning_service.py` | `48cf623842a5ad11a1d7e07fa97128a192f26c994b0bc319738f220b47d24438` |
| `agent/learning_runtime.py` | `bc278e3b4fd387ce0871abd98a27abbd71438fe1f596ebcce319e3d4d57241bc` |
| `agent/learning_artifacts.py` | `b7d6d3a95fa4310fadb78bd45dfb58e89b08d27d651678c52f437d25f4ac51f7` |
| `agent/learning_maintenance.py` | `0a2ade7b4b5d55d1771fa2aea8022c8bff77329bb4f6eb19a4045ba490bee41a` |

## 哪些能力原生已有，哪些边界仍需补齐

| 成果／能力 | 原生已有 | 当前插件／补丁已有 | 共享时仍需新增或明确 |
|---|---|---|---|
| 用户偏好与长期事实 | 每个 home 的 `memories/MEMORY.md`、`USER.md`；原生 memory 工具及写入保护。[N1]、[N2] | 外围按条目摘要记录来源项目，并按项目筛选。[P1] | 显式 `user` 与 `project` 共享范围、稳定条目 ID、修订／撤回事件；不能把 `USER.md` 文件名直接当作全局共享授权。 |
| 相关记忆召回 | provider `prefetch`、缓存预取和生命周期；provider 可接既有存储。[N3]、[N4] | Learning API 在观察投影时调用原生 prefetch；MCP 提供项目记忆视图。[P1]、[P2]、[L2] | 同项目身份映射、每次召回范围过滤、删除传播及去重。provider 本身没有通用跨 home 事务。 |
| 历史会话证据 | `session_search` 搜索当前库，或显式指定一个命名 profile 的只读库。[N5] | 外部执行观察投影为原生 `external-observation` 会话。[L2] | 如确有跨库历史需求，增加允许的库清单、项目过滤、来源和去重；无需为共享成果默认复制所有聊天。 |
| 技能及支持文件 | `external_dirs`、`create_dir`、技能解析／加载／修改；原生版本变更 ledger。[N6]、[N7]、[N10] | 精确快照、版本与交付 token、发布范围、MCP `skill_load`。[P2]、[L1] | 一处权威写入；原生候选携带 `base_version`；共享副本实际只读；冲突拒绝或重审。 |
| 技能采用与结果 | view/use/patch 计数、`on_skill_lifecycle`；加载即计 use。[N8] | 交付回执，以及独立的 `applied`／`verified`／`failed` 证据处理。[P3]、[L1] | 原生运行的真实工具／测试证据接入；精确技能版本关联；加载、采用、测试通过保持不同语义。 |
| 整理、归档与回滚 | 原生 Curator、`.archive`、ledger、blob 和快照。[N9]、[N10]、[N11]、[N12] | 补丁把 Curator 同步接入单写者维护，并读取原生技能变更历史。[L3] | 共享撤回／替换事件广播，发布版与归档状态一致；共享回滚应由权威方执行并再次发布。 |

以上最后一列是方案要求，不是声称上游缺陷，也不是本次已实现内容。

## 1. Memory／USER 与命名空间

原生 `get_memory_dir()` 每次解析当前 home；两个内置 target 只是 `memory` 与 `user`，分别落到 `MEMORY.md`／`USER.md`。系统提示使用会话开始时的冻结快照。官方明确建议每个 agent 使用独立 home／profile，需要共享时通过外部 memory provider；因此不应把两个运行进程指向同一个 home。[N1]、[N2]

当前原生 memory 工具还明确要求只保存适用于所有 session 的持久事实，任务中学到的流程、陷阱和针对某类任务的偏好放入相关 skill。内置 `USER.md` 既不是项目记忆数据库，也不是统一用户身份服务。项目级共享应在 provider／外围结构化 scope 层实现，不应把每个项目的事实复制进两端始终注入的内置文件。[N13]

内置写入并非毫无并发保护：`MemoryStore._mutate` 会持文件锁、重新读取、检查并写入；经审批的修改可携带完整 `matched_entry`，内容变化后返回 `stale_entry`。这些是**同一原生存储中的写入与过期检查**，不等于跨 home 的条目版本、事件去重、共享范围、删除广播或全局事务。[N2]

本插件的项目隔离是外围功能：`knowledge.reconcile()` 按 `[target, entry]` 摘要维护项目来源；混合来源改写不重新标成当前项目；`context_view()` 只有在明确项目一致时返回记忆。该逻辑对 `MEMORY.md` 与 `USER.md` 都适用，目前不能据文件名断言已有个人级共享命名空间。设计中的全局用户偏好，应新增显式 `scope=user` 及来源确认；项目事实维持 `scope=project`，默认不跨项目。[P1]

## 2. Memory provider 能复用什么

原生 `MemoryProvider` 提供 `initialize`、`prefetch`／`queue_prefetch`、`sync_turn`、session end／switch／pre-compress hooks、provider 自有工具以及 `on_memory_write`。初始化可获得 profile home、session、用户标识及逻辑 cwd；每个 agent/provider 实例绑定所属 profile，不能复用可变的全局绑定。原生同时只允许一个外部 provider。[N3]、[N4]

原生 agent 的 inline memory 工具执行成功后，由 `MemoryManager.notify_memory_tool_write` 调用 `on_memory_write`；replace/remove 的 `previous_content` 是在本地锁下选中的完整旧条目。它是很有价值的候选事件来源。不过通知在本地写入之后发生，provider 失败按非致命错误处理；接口不提供统一的全局条目 ID、版本 CAS、持久 outbox、跨 home ACK、去重、补偿或强制共享策略。`MemoryProvider` 也没有统一的 list/history/edit/delete 方法；这些可以由具体 provider 的工具或存储实现，但不能从抽象接口推断已有。[N3]、[N4]、[N14]

因此 provider 是原生可用的存储／召回适配点，不会自动解决本任务的发布与同步协议。薄本地 provider 可以把原生召回与成功写入候选接到既有 pilot 服务，保留原生会话生命周期，不引入第三方存储或第二套总结模型。若已有外部 provider，必须设计如何在同一 provider／其后端中组合，不能假设第二个 provider 会同时激活。工具证据与技能仍使用普通 hooks／MCP，而非强塞进 memory 接口。[N3]、[N4]、[P2]

Learning API 补丁的 `read(kind="context")` 返回内置记忆并拼接 runtime 缓存中的 provider recall，签名没有 query/project scope 参数；当前 `knowledge.context_view()` 明确将 `provider_recall` 清空，并标记 unscoped recall 被扣留。薄 provider 的接入必须新增／复用**结构化 user/project 选择接口**，返回可核验的 scope、来源与版本，不能删除这道过滤后直接透传 recall 文本。补丁 `project()` 确实调用原生 prefetch 和回合同步，但这仍是补丁适配，不是无补丁 Hermes 对外提供的统一知识 API。[L1]、[L2]、[P1]

### Provider 镜像的 P0 验收缺口

本次静态调用链检查已经发现以下差异；它们不能被“provider 支持 on_memory_write”一句话覆盖：

| 写入入口 | 固定版本中的路径 | 共享验收要求 |
|---|---|---|
| 模型调用 memory 工具 | inline executor 调 manager，成功后镜像；staged／失败结果不镜像。[N14]、[N4] | 正确提供来源、完整旧条目、幂等 ID；stage 不算 applied。 |
| `/memory approve` | shared approval handler → `apply_memory_pending` → store apply；这条调用链没有 manager/provider 通知。[N15]、[N13] | 审批真正提交后生成候选事件；不能靠原先 staged 工具事件推断共享已完成。 |
| Journey 的 memory edit/delete | `learning_mutations._mutate_memory` 直接调用 `load_on_disk_store()._mutate`；有本地锁／过期检查，没有 provider 通知。[N16] | UI／CLI 人工修改和删除必须进入同一可靠事件合同。 |
| Web memory reset | 指定 profile 下直接 unlink 内置文件；没有逐条 provider 通知。[N17] | 明确 reset 是本地清空还是共享撤回，按所选语义记录 tombstone；不得默默猜测。 |
| 手工文件编辑、恢复／导入 | 本次没有核验所有路径。 | 不承诺自动覆盖；需要来源标记及受控对账，不能把一次文件 diff 自动当成用户授权共享。 |

另外，`MemoryStore` 的 prompt snapshot 在 load 时冻结，`format_for_system_prompt()` 明确不返回实时文件内容。因此旧内置 `USER.md` 偏好可能与当轮 provider 新偏好同时出现。实现前必须确定同一偏好的权威位置、版本／撤回策略、当前会话中的冲突呈现以及下一 session 的迁移。应避免把共享偏好同时长期复制成两个来源的强指令，也不能为了即时同步中途重建原生 prompt prefix。仅改变 recall 文本顺序并不能证明冲突已解决；该场景必须端到端验收。[N18]

## 3. `session_search` 的可复用边界

当前版本支持 `profile=<name>`：通过原生 profile 解析器定位其 `state.db`，用 `read_only=True` 打开；每次调用仍只针对一个库。裸 session ID 在本 profile 未命中时不会自动扫描其他 profile。工具签名提供 query、role、日期、session/window 等参数，没有 project/cwd 过滤或多个库的聚合参数。[N5]

profile 不是任意路径参数：命名 profile 按默认根下的 `profiles/<name>` 解析。研究时的 pilot 位于独立的 `~/.hermes-codex-learning/pilot`，不能仅凭这个目录名就认定原生 `profile=pilot` 能访问它。两个 home 的物理会话库保持独立；需要跨库时应通过显式受控适配，复用原生只读搜索实现，并在返回前按项目和来源过滤，而非复制／拼接 `state.db`。[N5]

原生搜索默认隐藏 `kanban`、`subagent`、`tool` 来源，降低 cron 排名；这不是共享范围 ACL。相同任务在 Codex 原始事件、pilot 投影、原生执行记录中出现时，仍需要来源 ID／投影关系去重，不能用命中数当作独立成功次数。[N5]、[L2]

## 4. `external_dirs`／`create_dir` 与 Curator

官方技能文档明确：`external_dirs` 中的技能可被原地 patch、写文件或删除；只要进程拥有文件权限，它就不是写保护边界。`create_dir` 仅改变新技能落点，并加入技能发现；文档同时明确 bundled sync、hub 与 Curator 继续作用于 profile-local skills。解析优先级为 project → local → create_dir → external；同名副本可能遮蔽已发布版。[N6]

源码需分清三个层次，不能一概称“外部技能只读”：

1. 原生 Curator 的候选扫描只遍历本 home 的 `skills`；过滤 external/project 路径，且管理资格依赖 `.usage.json` 的 `created_by=agent` 等显式标记。独立 `create_dir` 中的新技能不会因此自动进入此本地扫描。[N7]
2. `archive_skill` 拒绝 external；后台 delete guard 也拒绝 external、pinned、非管理对象等。[N7]
3. **同一后台 guard 的注释明确说明内容写入不按 ownership 禁止**，外部技能的 patch 不具备与 delete 相同的所有权阻断。Curator 提示与候选集要求不碰外部技能，并不构成所有写入路径的文件系统只读保证。[N7]、[N9]

每技能修改锁在 `当前 home/skills/.locks/`；usage、ledger 也跟随当前 home。两个 home 指向同一 external/create_dir 时，可能锁住不同文件并写同一技能，且各自保留局部计数与审计。目录挂载不构成共享锁、全局审计或冲突合并。[N8]、[N10]

建议原生侧消费发布快照；候选改进保存到自己可写的局部空间，再提交权威方。若采用 external_dirs 展示发布技能，应限制实际修改能力并检查同名 shadow。不要让两套 Curator 同时维护共享目录，也不要把 `create_dir` 当作完整 Curator 托管配置。[N6]、[N7]、[N10]

## 5. 使用计数与真实效果反馈

原生 `skill_view` 成功后同时增加 view/use；`bump_use` 的语义是加载进 prompt／被 assistant 引用，生命周期事件名为 `loaded`。原生 `.usage.json` 以技能名为键，维护最近活动、计数、patch generation 等，是 Curator 活跃度信号；它不证明技能被采用、测试通过，亦不提供每次执行绑定精确内容版本的成果评价。[N8]

Learning API `record_usage` 要求 `stage=body_delivered` 与 source_event，然后调用原生 bump_view/bump_use；返回还注明 telemetry 是 best effort。真实采用／验证是本插件单独的 `feedback.py`：初始采用为 self-reported，验证依据宿主测试事件；测试成功也保留 `effectiveness=not_inferred`，没有把相关性冒充因果改善。[L1]、[P3]

原生接入应补充真实宿主事件来源、skill version、task/session/tool-call 关联和幂等 ID。`post_tool_call`／`on_skill_lifecycle` 可以复用为采集入口，但只有 `loaded` 时不能写成 `verified`；只有 terminal 调用成功也不必然表示测试成功。上报事实可复用现有反馈处理，具体原生适配仍待实现和验证。[N8]、[P3]

## 6. Ledger、archive、rollback 的保障范围

原生已有成熟的局部恢复素材：技能修改 ledger、内容寻址 blob、归档目录、Curator 快照和报告；Curator consolidation 仅启用 skills 工具，避免 terminal 直接改文件绕过 ledger。ledger 和计数使用跨进程文件锁。[N8]、[N9]、[N10]

但保障有清楚的范围：

- ledger 记录属于 best effort，捕获／追加失败不会阻断普通修改；Curator consolidation 的预快照失败也不会阻断整理。不能承诺“所有成功写入都已有可用备份”。[N9]、[N10]
- `rollback_entry` 会拒绝任何位于当前 `HERMES_HOME` 外的 before/after 路径。外部共享技能即使生成了本 profile 的 ledger/blob，也可能无法通过该回滚接口恢复。[N11]
- 单条回滚先检查所需 blob、保存当前安全副本，再恢复历史 before；它没有验证“当前内容仍等于目标事件的 after”的跨 home CAS。整树回滚以本地 skills 为范围，先做安全快照再暂存／恢复，不能当作在线多 writer 的合并。[N11]、[N12]
- 自动 Curator run claim 位于本 home，过期 claim 可接管，锁目录创建失败时可继续运行；它不是跨 home 的分布式写入租约。补丁维护函数额外选择 synchronous=True，让 pilot 单一写入者等 Curator 完成再继续。[N9]、[L3]

共享方案可复用权威 home 的这些原生素材与命令；对外发布撤回／替换后的新版本。消费者本地的 usage、archive、ledger 可保留为局部事实，不能仅把文件合并成全局事实。

## 仍需新增的最小协议与验收

以下是待实现合同，不应表述成现有原生能力：

- 统一成果键：`user_id`、稳定 `project_id`、`artifact_id`、`artifact_version`，以及 `source_executor/session/event`；cwd 是输入线索，重命名、worktree、远端路径需明确身份规则。
- 原生候选提交：带 `base_version`、候选差异、来源证据、共享范围；权威方在锁下校验基线，冲突保留为候选，不覆盖较新公共版。
- 可靠事件：本地持久 outbox／ACK／重放去重、删除 tombstone、来源链，防止“共享召回 → 两端再学习同一内容 → 再传播”的循环。现有 memory 通知及 skill 生命周期 hooks 均不能单独保证这一合同。
- scope enforcement：每次读写都按 user/project/publication 范围检查；个人偏好显式确认；跨项目原始 MEMORY/provider recall 默认不返回。
- 共享副本只读、唯一整理者、同名解析检查、版本与回执一致；发布撤回后旧版本不可继续作为当前版交付，历史证据仍可追溯。
- 原生真实结果适配：加载／采用／测试／失败分开记录；失败版本限制复用与后续复盘沿用现有外围流程。
- memory 各入口覆盖：inline、pending approve、Journey edit/delete、reset 与恢复／导入的范围语义分别验证；provider hook 不作全覆盖承诺。共享偏好与冻结内置快照的冲突、scope recall 替代当前无范围 provider 文本过滤，均为接通前 P0。

实施前至少在临时 home 验证：双端同时提交同一基线只接受一个公共后继；跨项目不召回；重复事件不重复计 use；断线重放不重复学习；删除／归档／替换传播；外部技能修改确实被拒；精确版本交付；原生失败测试能关联到所加载技能；回滚后公共版本与所有读端一致。本研究只做静态源码核验，未执行这些验收，也未核实任何生产内容是否已共享。

## 来源

N 类链接均固定官方 commit `3b421578588243f920886e6fe79a6faa87ea026e`。另在线交叉核对了官方 [Memory Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers)、[Persistent Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/)，以及官方仓库的 [Skills 文档](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/skills.md)；有变动风险的结论以固定源码为准。

[N1]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/website/docs/user-guide/features/memory.md#L13-L33
[N2]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/memory_tool_store.py#L350-L394
[N3]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/memory_provider.py#L84-L238
[N4]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/memory_manager.py#L774-L855
[N5]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/session_search_tool.py#L434-L477
[N6]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/website/docs/user-guide/features/skills.md#L422-L464
[N7]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/skill_manager_guards.py#L164-L202
[N8]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/skill_usage.py#L470-L534
[N9]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/curator.py#L931-L962
[N10]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/skill_ledger.py#L1-L99
[N11]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/skill_ledger.py#L557-L633
[N12]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/curator_backup.py#L406-L481
[N13]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/memory_tool.py#L353-L393
[N14]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/inline_tool_executors.py#L144-L164
[N15]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/hermes_cli/write_approval_commands.py#L128-L142
[N16]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/agent/learning_mutations.py#L85-L119
[N17]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/hermes_cli/web_routers/ops.py#L535-L555
[N18]: https://github.com/NousResearch/hermes-agent/blob/3b421578588243f920886e6fe79a6faa87ea026e/tools/memory_tool_store.py#L649-L652
[P1]: https://github.com/cYz26/codex-hermes-plugin/blob/fe0abd386c9244387d58f9e1e613bbe0b9c0d19d/src/hermes_codex_learning/knowledge.py#L16
[P2]: https://github.com/cYz26/codex-hermes-plugin/blob/fe0abd386c9244387d58f9e1e613bbe0b9c0d19d/src/hermes_codex_learning/mcp.py#L28
[P3]: https://github.com/cYz26/codex-hermes-plugin/blob/fe0abd386c9244387d58f9e1e613bbe0b9c0d19d/src/hermes_codex_learning/feedback.py#L158
[L1]: #本地补丁源码定位
[L2]: #本地补丁源码定位
[L3]: #本地补丁源码定位

### 本地补丁源码定位

以下行号对应核验基线中记录 SHA-256 的本机候选文件：L1 `agent/learning_service.py:401`，L2 `agent/learning_runtime.py:16`，L3 `agent/learning_maintenance.py:17`。

补充精确位置：`tools/memory_tool.py:38–40`（home 路径）；`tools/memory_tool_store.py:470–489`（过期条目保护）；`agent/memory_manager.py:283–293`（单外部 provider）；`website/docs/developer-guide/memory-provider-plugin.md:124–134,215–221`（cwd 与写入旧内容）；`tools/session_search_tool.py:21–32,603–649`（来源过滤与单库调度）；`hermes_cli/profiles.py:292–302,442–454`（profile 路径）；`tools/skill_usage.py:47–56,215–237,278–307,645–708`（本地范围与归档）；`tools/skill_manager_tool.py:84–105,746–759,811–831`（锁与 best-effort ledger）；`tools/skills_tool.py:758–780`（view 即 use）；`agent/curator.py:848–877,1124–1149,1187–1231`（候选、工具与 run claim）；`agent/learning_service.py:36–45,572–587`（补丁单写者锁与交付计数）。所有原生位置均属于上述固定 commit；Learning API 位置属于已记录 SHA-256 的补丁文件。
