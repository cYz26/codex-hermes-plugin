# Codex 执行、Hermes 学习：含可见反馈与模型配置的实施方案

## 1. 基础架构与实现边界

采用官方 **Hermes v0.22.6 / `f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab`** 从头实现；不引入之前定制版的代码、配置、数据或维护脚本。

```text
Codex App 执行任务
  → hooks 自动采集
  → 独立后台服务持久排队
  → Hermes Learning API
  → 原生复盘、记忆、技能、Curator
  → 学习提示＋后续任务自动召回
```

- Codex 核心不改；Hermes 增加版本化 `ctx.learning(version=1)`，提供观察会话、复盘、成果读取、使用反馈和维护能力。
- 核心复用原生提示词、模型路由、工具、写入保护和技能锁；外围插件负责采集、队列、通知、发布和 Codex 适配。
- 首版采用 macOS `launchd` 后台服务、Unix socket、in-process Hermes 插件，独占 `/Users/cy/.hermes-codex-learning/pilot`。
- 真实 App hooks 为必需采集来源；不依赖接管桌面内部 App Server。事件先落盘再投递，按真实会话、回合、工具调用身份去重。
- 保留原生阈值；明确终态后空闲 120 秒可补充复盘，只处理未审阅证据，补充触发至少间隔 10 分钟，并发为 1。

## 2. 让用户和 Codex 都感知学习发生

按你选择的 **“聊天提示＋系统通知”** 实现两条反馈通道。

**给用户的提示**由真实学习结果生成，示例：

| 实际状态 | 展示内容示例 |
|---|---|
| 复盘正在执行 | `Hermes 正在整理本轮经验` |
| 成果已经生效 | `Hermes 已学习：新增 1 条记忆，更新「项目验证」技能` |
| 原生规则要求暂存 | `1 项记忆修改等待批准，尚未生效` |
| 后续任务加载成果 | `本轮已加载更新后的「项目验证」技能` |
| 失败或结果不明 | `本次学习未完成，已保留执行记录` |

“已加载”不显示成“已经验证有效”；复盘没有产生变化时保留记录，默认不弹系统通知。

**给 Codex 模型的反馈**是简短的新增成果摘要，包括作用域、版本和读取入口。相关技能正文仍按需加载，避免每次塞入完整学习报告。

交付方式：

- 通过 hooks 的 `systemMessage` 向用户提示，使用 `additionalContext` 告知模型；后者只发送增量，设置约 256 tokens 的单次上限。
- 活跃聊天在下一个安全点显示；空闲时后台直接发 macOS 通知，聊天在下次交互时补收结果。
- 系统通知由程序模板生成，不另外调用模型写通知，也不为通知唤醒一轮 Codex。
- 通知、模型上下文和学习产物使用独立消费记录，服务重启后避免重复播报。

这是宿主能力边界：官方 hooks 在无活跃回合时等待下一次用户输入，不能保证后台完成瞬间就向空闲聊天插入消息；`systemMessage` 当前以 UI 警告形式呈现，实际样式需在目标 App 验收。[OpenAI Hooks 文档](https://learn.chatgpt.com/docs/hooks)

Hermes 原生同样从成功的记忆、技能工具结果生成提示；新增学习接口应保留这套事实来源，并补充暂存、失败等结构化状态。[原生提示实现](https://github.com/NousResearch/hermes-agent/blob/f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab/agent/background_review.py#L1237)

## 3. Hermes 模型 API 如何配置

**模型和认证归 Hermes 管理，桥接插件只提交证据、读取结果。** Codex 的任务模型可以与 Hermes 学习模型不同。

首版推荐独立 profile 使用以下配置：

```yaml
model:
  provider: openai-codex
  default: gpt-6.1-sol
  api_mode: codex_responses

agent:
  reasoning_effort: medium

auxiliary:
  background_review:
    enabled: true
    provider: auto
    model: ""

display:
  memory_notifications: "on"
```

这里的 `auto` 表示继承 **Hermes 学习会话**的模型，并不是读取 Codex App 当前选择。`medium` 是首版建议默认值，可以独立于 Codex 的任务推理强度调整。

有三项配置边界：

- **认证：** 在新环境中通过 Hermes 官方认证流程登录 `openai-codex`；凭据由 Hermes 保管和刷新，外围插件不读取或维护 Codex App 的认证文件。
- **执行方式：** 使用 Hermes 原生 `codex_responses` 工具循环，不设置 `openai_runtime: codex_app_server`。这样复盘中的 `memory`、`skill_manage` 由 Hermes 执行。
- **推理强度：** 同模型复盘继承父会话设置，单独填写 `auxiliary.background_review.reasoning_effort` 不生效；因此调整专用 profile 的 `agent.reasoning_effort`。[官方路由源码](https://github.com/NousResearch/hermes-agent/blob/f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab/agent/background_review.py#L253)、[同模型配置说明](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory#same-model-review-reasoning)

如以后希望独立 API Key 计费、使用其他供应商或自建 endpoint，通过 Hermes 原生 provider 配置切换即可，无须修改桥接协议。更换后的模型必须通过真实工具调用、记忆和技能写入验收。[Hermes Provider 文档](https://hermes-agent.nousresearch.com/docs/integrations/providers)

首版先保持上述模型建立质量与消耗基准，暂不同时引入较便宜的复盘模型，避免无法判断效果差异来自集成还是模型。

## 4. Token 会增加在哪里，怎样控制

**不能把全部 Hermes 复盘消耗都算作新增成本，因为原生 Hermes 本来就会复盘。** 真正需要比较的是以下差额：

| 环节 | 相比原生的影响 |
|---|---|
| 事件采集、入库、队列、通知 | 不调用模型，本身不消耗 LLM tokens |
| 原生复盘 | 本来就存在；相同次数不代表相同缓存成本 |
| Codex → Hermes 的上下文转换 | Hermes 需要读取外部执行历史，不能保证复用 Codex 的缓存 |
| 空闲补充复盘 | 增加原生阈值之外的调用，是明确的新增消耗 |
| 经验返回 Codex | 摘要和技能正文会占用 Codex 输入上下文；原生 Hermes 也有记忆、技能加载成本，需比较差额 |
| Curator、压缩、外部记忆服务 | 按实际启用和调用统计，不能全部归为桥接开销 |

**最需要关注的是缓存。** 原生同模型 review 特意保持父会话的提示词、工具和推理配置一致，争取复用已有缓存。本方案中的 Hermes 没有执行过 Codex 主任务，首次复盘没有同样的预热条件。同一模型、同一账户也不等于缓存可直接复用。[Hermes 缓存实现](https://github.com/NousResearch/hermes-agent/blob/f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab/agent/background_review.py#L839)

因此，**总输入 tokens 接近时，未缓存输入仍可能更多**；Token 数、缓存命中和实际费用应分开统计。OpenAI 的缓存复用依赖匹配的前缀及兼容请求设置。[OpenAI 缓存诊断](https://developers.openai.com/api/docs/guides/prompt-caching/diagnostics)

首版控制措施：

- 不让 Hermes 重做业务任务，不增加“先总结再复盘”的额外模型回合。
- 同一批证据不被原生触发和空闲补充重复复盘；新证据提交不等于每次都调用模型。
- 保留原生输入预算和循环限制；达到预算应报告未完成，不当成成功。`max_input_tokens` 限制的是整次复盘累计输入，`0` 表示不限，不能用来关闭复盘。
- 通知采用程序模板；成果增量注入，技能正文按需加载。
- 分别记录 Codex 执行、Hermes 复盘、其他辅助调用的输入、输出、缓存和调用次数。拿不到的 usage 标为未知，不填零，不重复累计缓存 tokens。

不预先承诺“只增加 10%”或“必然翻倍”。先比较相同任务、相同学习触发条件下的实际数据，再单独测量空闲补充的收益与成本。OAuth 路由也不视为免费额外额度；实际额度消耗以账户返回为准。

## 5. 验收与升级

首版必须完成真实 App 单项目闭环：

1. 聊天 A 正常执行任务，后台自动学习，无需手动要求总结。
2. 真实成果、暂存、失败与无变化显示准确；空闲完成有系统通知，下一次聊天获得增量上下文。
3. 聊天 B 自动召回相关经验，实际加载正确技能版本，并记录使用和验证证据。
4. 通知过程没有额外模型调用；同一事件不重复学习、重复计数或反复播报。
5. 验证技能锁、重复投递、迟到结果、写后崩溃、服务重启、provider drain 和 Curator 维护。
6. 用“原生阈值基准”和“原生阈值＋空闲补充”分别报告学习质量、延迟、tokens、缓存命中和可获得的费用/额度信息。

Hermes 升级只重放本项目的学习扩展补丁并执行原生回归；Codex 升级主要验证采集和提示适配。数据迁移单独验证、保留快照。更新方案文档时移除旧定制版依赖，保留历史探针并明确其验证范围。

以上为规划，尚未修改配置、部署通知服务或执行真实模型验收。
