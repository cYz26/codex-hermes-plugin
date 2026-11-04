# 最小 Hermes 学习扩展边界：设计审查

2026-11-03；只读核对本机 Hermes `v0.22.5` / `bf0203a794a07aac3ff764972ab9087baa4e4f4e`。本轮只新增本文，不改生产源码、配置或服务，不运行模型。`ctx.learning(version=1)` 及本文方法均为**建议新增合同**，不是现有 API。

## 判断

可以把对插件的稳定入口收敛为一个版本化 facade，同时把 Codex 适配、传输、持久队列、空闲调度、展示和成果发布放在插件。**稳定入口不等于一行包装、一个方法或一个文件 patch。** 真正需要稳定的是外部回合如何进入原生学习、运行何时完成、失败后哪些事实仍成立；如果插件仍需自己构造 AIAgent、改计数、绑定 MemoryProvider 或猜测 review 完结，就没有形成有效边界。

本机已有相同方向的先例：`PluginContext.llm` 是宿主拥有的模型 facade，`subagent_lifecycle` 提供版本化、不可变合同与可序列化 handle，`delegation_backends` 是面向 owner 的 facade。新增学习入口可沿用这种结构，不向插件暴露内部 agent/registry。[现有入口](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/hermes_cli/plugins.py#L376)、[lifecycle v1 值合同](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/subagent_lifecycle.py#L23)

上述先例仅说明 facade 的组织方式：现有 subagent_lifecycle 需要 active parent，结果留在进程内，重启后的 reconnect 明确失败，不能直接当学习服务或 durable job 使用。学习 scope 必须有自己的显式生命周期与持久收据。[该限制](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/subagent_lifecycle.py#L270)

## 现有插件 API 已能承担的部分

| 已有能力 | 适合承担 | 不能据此推断 |
|---|---|---|
| `register_tool`、`register_cli_command`、`register_command` | 只读证据工具、插件命令、独立投递/管理进程入口 | 注册 Hermes tool 不会自动把它暴露为 Codex MCP，也没有自动获得外部回合导入能力。 |
| `register_hook`、`register_middleware` | 观察工具/回合、有限上下文注入、工具策略 | 普通 hook 没有受支持的 background write-origin 字段，不能凭缺失回调判断后台身份。 |
| `dispatch_tool` | 真正调用 native skill_view 等现有工具 | headless 场景不保证有 parent agent；不是 memory/provider 完整生命周期替代品。 |
| `inject_message` | 向合法现有会话投递普通输入 | 不是无损轨迹导入，也不是 durable review 完结合同。 |
| `register_auxiliary_task` | 注册模型路由/配置条目 | 只登记配置，不自动获得原生 review 调度、工具 guards 或学习生命周期。 |
| `register_memory_provider` | 声明 provider 实现 | 当前此处是 inert registration；激活由 memory.provider/loader 管理，不等于已 initialize/sync/shutdown。 |

依据：[工具注册](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/hermes_cli/plugins.py#L462)、[CLI/command/dispatch](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/hermes_cli/plugins.py#L643)、[provider 注册](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/hermes_cli/plugins.py#L733)、[auxiliary 配置注册](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/hermes_cli/plugins.py#L863)、[实际工具 hook payload](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/hermes_cli/plugins.py#L1941)。

## 建议合同：一个 facade，六个主操作

```python
learning = ctx.learning(version=1)  # 新接口示意，拒绝不支持的版本
```

| 主操作（示意） | 最小合同 |
|---|---|
| `open(source_scope, request_id)` | 创建/恢复外部观察 scope；返回不可变 handle/capabilities。绑定插件获准的 profile，不接受任意 HOME、凭据或跨 profile agent 对象。 |
| `observe(handle, observation, event_id)` | 接收规范化外部回合；由核心验证、幂等入库、推进原生计数、安排 provider 同步并返回 receipt 与 review intent。revision/冲突/不完整状态明确；不执行源任务，不额外调用学习主模型。 |
| `run_review(handle, intent_id, attempt_id)` | 执行核心签发的固定快照/作用域 intent，始终保留指定的原生无人值守策略；返回可查询 attempt handle，终态由核心生成。插件不能传入 private prompt、write_origin 或 `explicit=True`。 |
| `read(handle, typed_request)` | 有界读取：`ContextRequest`、`SkillRequest`、`ReceiptRequest`、`AttemptRequest`、`UsageRequest`。使用明确 tagged union，返回值对象；不开放任意对象反射/SQL。context recall 可能调用 provider，不应承诺零成本。 |
| `record_usage(handle, event)` | 对真实加载/采用/验证记录来源、技能版本及幂等 ID；原生 load/use 更新由核心负责。采用/验证不等同于 load，不自动转换为 Curator 分数。 |
| `close(handle, reason)` | 明确 end/rotate/soft release 的生命周期；返回 provider drain/未完成状态，不能把 Python 引用被回收当作正常结束。 |

六个方法只是建议规模，typed request 的语义仍需要文档和测试，不能通过一个 `execute(action, **kwargs)` 假装合同更小。取消/事件订阅可作为后续 capability；v1 若提供取消，须独立定义“取消请求已接受”与“已停止”，不能以 close 结果默示所有外部写入已回滚。

`observe` 的输入建议统一为 runtime-neutral `ObservedTurn`：source IDs、原始作者、消息/工具关联、completed/failed/interrupted、附件引用/hash、覆盖缺口与 schema 版本。Codex hooks/app-server 版本适配放插件；核心可共享现有 projector 的规范化逻辑，但由核心按已验证的 completed item 计算活动，不接受客户端一个任意 `tool_iterations=100` 就直接增加计数。真实外部轨迹必须保持 external provenance。[现有 projector](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/transports/codex_event_projector.py#L48)

## 核心最小责任与插件责任

| 必须由核心掌握 | 可留给外围插件 |
|---|---|
| profile/来源授权、观察会话与 transcript 合同；receipt 去重和真实计数的一致提交 | Codex hook/MCP/历史格式适配、证据采集与脱敏、raw artifact 保存及上传重试 |
| 现有 nudge 的计数/reset/禁用/完成条件；签发带范围和快照版本的 review intent | 合并尚未执行的队列项、并发额度、空闲窗口、重试退避、operator UI |
| provider 初始化、作用域、作者归因、turn-start/recall、sync/prefetch、end→switch→shutdown 顺序与实际状态 | 根据公开结果安排下一步；显示各 provider 状态，不手工调用其 hooks |
| 原生 review prompt、路由、工具白名单、read-before-write、background origin、原生 memory/skill guards | 决定何时执行已授权 intent；空闲补充策略只能请求明确的新 trigger，不能伪造阈值 |
| 执行 attempt 的真实 outcome、部分 mutation、pending、成本、可恢复收据与 unknown | 持久 job 清单、跨进程调度、通知、缓存；job 状态是核心事实的投影 |
| skill_view/native usage、memory/provider 读取、Curator 原生规则、跨 writer 协调机制 | 按版本发布完整技能目录、Codex 召回与缓存、实际采用/验证 evidence |

`_finish_codex_turn` 不宜直接变成 facade：它假设 run_conversation 已增加用户/记忆计数，混合 compaction 与 token 账；普通 finalizer 也负责很多业务回合收尾。应提取共享“已观察回合→学习 intent”组件，现有 Codex runtime 与新 facade 调用同一语义，差异通过回归固定。[现有 finish 假设](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/codex_runtime.py#L701)、[普通 nudge/provider 触发](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/turn_finalizer.py#L763)

## 为什么只公开 `_spawn_background_review` 不够

这个函数依赖已经初始化的父 agent、memory store、session、模型/凭据与当前 profile 上下文，只负责启动/延迟复盘。fork 自身 `skip_memory=True`，不会完整运行外部 provider 生命周期；其 transcript 持久化也被禁用。它没有替外部观察完成原生计数、规范化入库或 source provenance。外部插件若自行补这些前置状态，会依赖更多私有字段，升级成本反而扩大。[fork 初始化](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/background_review.py#L923)

当前 review summary 也不构成执行结果协议：actions 汇总异常会变为空列表，只有非空 actions 才通知。新 facade 必须返回结构化 `run_status` 和独立的 `applied[]/staged[]/uncertain[]`，明确 no_change 的证据；不能解析本地化日志或把“没有 summary”当作无变更。[当前 summary 分支](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/background_review.py#L1257)

同模型路由同样由核心解释：`auto` 绑定观察 scope 的学习模型/profile runtime，不自动取得源 Codex App 的凭据或缓存。源任务 usage 是来源元数据，review usage 是真实 Hermes 支出，两者分账。插件可以聚合显示，不能用重放 transcript 再伪记一次模型成本。[路由解析](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/background_review.py#L253)

## 队列与崩溃：最低诚实保证

插件持久队列只能保证请求不会轻易丢失，**不能单独保证 exactly-once learning**。至少存在三个窗口：原生文件已写但 plugin receipt 未落盘；provider 已收到请求但网络确认丢失；多个写入完成一半时进程终止。盲目重跑模型可能重复添加记忆、产生不同技能或覆盖新的版本。

因此即使主 job queue 在插件，核心仍需最小的 durable admission/attempt receipt 与幂等键，区分 never-started、running、finished、unknown/reconciling；observe 的去重、计数和 intent 必须处于一致提交边界。**这保证的是受理/计数去重，不自动等于文件 mutation 或第三方服务事务。** 最小核心收据可以只记录 source event ID、已提交 session revision、已应用计数增量、native intent 与 attempt 状态，不必将插件整个 job queue/schema 搬入核心。session 写入与计数/收据若无法同事务提交，就必须增加明确恢复协议，不能仅靠进程内计数宣称幂等。outcome 丢失的已开始 attempt 不应自动当作未执行重试。技能 ledger 是 best-effort audit，不是 transaction gate；要提供更强恢复，必须进一步扩展原生 mutation receipt/operation ID，或诚实返回 unknown 待核对。[技能账本声明](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/tools/skill_ledger.py#L1)

现有 `MemoryManager.sync_all` 在后台 serialized worker 排队，方法返回不表示远端已保存；shutdown 只做有界 drain，并有 abandoned-write 状态。facade 的 provider 结果至少区分 enqueued/delivered/failed/unknown，并对不提供幂等/回执能力的 provider 明示降级。不要要求插件捕捉 provider 对象、绕过脱敏或重做 shutdown 顺序。[异步 sync](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/memory_manager.py#L441)、[end→switch 顺序](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/memory_manager.py#L633)、[bounded drain](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/memory_manager.py#L858)

单插件队列也不能串行其他 writer：Curator、普通 CLI/tool、同一 profile 内其他会话和 pending 审批应用都可能修改相同文件。核心需共享协调协议/冲突检测；若 v1 仅依赖专用 profile 的单 owner，应写成受限部署前提，不能宣称全局串行。外部 provider 不在本地文件锁事务内。

## Curator 自动维护需要宿主接线

构造 AIAgent 或打开 headless 学习 scope，并不天然启动 Curator 自动 tick。当前 tick 由 gateway housekeeping、CLI 启动维护或 serve 的 session maintenance 驱动；serve 还会在同 profile 已有 gateway 时让出维护责任。新 facade 应复用所在宿主的原生维护调度，并登记学习活动/写入状态；独立 plugin worker 部署则需由 facade 管理同一维护入口，或新增明确的 maintenance capability，定义 profile owner、idle/interval、暂停与并发规则。不能让插件定时执行 `hermes curator run` 来冒充原生自动策略：显式 run 绕过正常 due gate，idle 判定本身由调用方负责。[gateway tick](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/gateway/run.py#L4369)、[CLI 启动维护](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/hermes_cli/cli_tui_runtime_mixin.py#L416)、[serve owner/idle 判定](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/hermes_cli/web_server_sessions.py#L502)、[原生 due gate](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/agent/curator.py#L160)

新增验收：独立 headless worker 在没有 CLI/serve/gateway 窗口时仍有明确维护 owner；首次播种、尚未到期、忙碌、暂停均不误跑；满足原生条件后实际运行；宿主重启和同 profile 多进程不会重复调度；归档/恢复与使用反馈及发布协调。仅验证技能被创建、或手动 run 成功，不能验收自动 Curator 已接通。

## 使用反馈与读回的最小边界

已有 `ctx.dispatch_tool("skill_view", ...)` 可先用于真实加载，它会原生 bump view/use；不必为了方法整齐重写读技能逻辑。新 facade 若提供带版本的 SkillRequest，可包装同一 handler并返回 canonical name/hash/read receipt。不可把扫描候选、定期导出当作 use。若 Codex 读取发布副本，再调用 record_usage，须用相同使用事件 ID 去重，避免一次加载在 read 与 report 两头各计一次。[skill_view 原生行为](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/tools/skills_tool.py#L737)

`record_usage` 的“已验证通过”是外部证据，不等于原生 `bump_use` 已提供成功率指标。应原样记录来源及技能版本，供后续原生学习；不要用这些信号直接伪造 `created_by`、last_used 或 Curator 评分。模型 token 统计也只能来自核心真实执行结果，插件统计可保留来源维度。[native load 定义](https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/tools/skill_usage.py#L493)

## 不应公开的对象与建议落地范围

不公开 `AIAgent`、`SessionDB`、`MemoryManager/Provider` 实例、mutable messages/counters、ContextVar/锁/线程、模型 client/credentials、review private prompt 或可任意设置的 write-origin；也不要让插件持有 `ctx._manager._cli_ref` 作为 owner。公开内容应为版本化 schema、immutable handles、receipts、typed errors/capabilities；未知协议版本拒绝，新增可选字段走兼容策略。scope 的结束/重开与进程退出应有明确语义，headless/gateway/multiprofile 都不能依赖交互式 CLI 引用。

最小可维护实现预计涉及 PluginContext 注册、一个 learning facade/service 模块、学习计数/完成策略提取、review 结果合同、最小持久收据、provider 状态与写入协调接缝以及回归测试。**这是一个受支持扩展面，内部可跨多个模块；插件升级只依赖此合同，不依赖这些模块布局。** 初版不必建全新 HTTP gateway、重写 agent loop 或搬走 Curator；插件可用现有 CLI/tool 能力承载外部 transport，核心只承诺学习语义与事实。

验收应覆盖：重复 observe 不重计、失败/中断与有限模式差异、原生 prompt/guards 不变、一次 applied+staged+failed 的混合结果、provider enqueued 后崩溃、ledger 写失败、与 Curator 并发、实际 skill load 去重、profile 隔离与软/硬关闭。若这些仍由插件猜测，facade 虽只有一个入口，也没有降低升级风险。
