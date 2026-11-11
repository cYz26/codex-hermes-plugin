# 复盘恢复与重试（0.8.0）

针对用户在 2026-11-11 提出的复盘中断恢复需求，0.8.0 在 Hermes v0.23.0／v0.23.1 的 Learning API 补丁和串行桥接 worker 中加入受控重试。采集、原生复盘提示词、模型配置、写入保护、资料准入、发布检查和 Curator 的职责保持原有合同。

## 复用官方机制

本次核对的官方基座为 v0.23.0、提交 `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`。原生 `NativeLearningRuntime.review` 仍经 `_run_review_fork` 执行复盘，并保留准备、结束、取消、非交互写入保护和本地模型闲时判断。

| 官方能力与原始源码 | 本次使用方式及适用范围 |
| --- | --- |
| [conversation_loop 的请求级重试](https://github.com/NousResearch/hermes-agent/blob/2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19/agent/conversation_loop.py#L1761) | 继续由原生执行器处理每次 API 请求；截图的 429 已走过三次请求尝试。整轮复盘的冷却恢复在其终态之后调度。 |
| [cancel_background_review_for_live_turn](https://github.com/NousResearch/hermes-agent/blob/2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19/agent/background_review.py#L129) | 保留原生取消与请求结束握手。该能力服务于前台优先和会话结束，不保存复盘执行断点；恢复由持久化收据决定。 |
| [ReviewIdleQueue](https://github.com/NousResearch/hermes-agent/blob/2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19/agent/review_idle_queue.py#L1) | 适用于托管本地模型的闲时延期，按会话合并到最新快照，状态存在进程内存中。插件继续使用原生闲时判断；当前云模型的限流恢复时间和次数由持久化队列承担。 |
| [Learning API 的 observation、intent、attempt 合同](../patches/v0.23.0/native-learning-api-v1.patch) | 在已有持久日志上添加有来源关联的后续意图。原尝试重复调用仍返回原收据，`unknown` 保留待核对，已有写入和失败收据不删除。 |

## 自动恢复

HTTP 429、500/502/503/504、暂时性连接故障、超时，以及原生已明确记录终态的中断，可进入重试队列。默认冷却 5、10、20 分钟，最多重试三次；原始尝试与重试合计最多四次。等待时 worker 继续处理其他执行证据和复盘，计时保存在桥接 SQLite 中，重启后不重新计算已有冷却期限。

新 worker 分页读取原生日志中尚未恢复的失败记录，因此部署前的失败也能被发现。每页最多 50 条，游标持久保存并循环核对，避免遗漏迟到终态。下一次执行通过原生 API 创建带 `retry_of`、`retry_root` 和 `retry_index` 的后续意图；原意图与原尝试保持原来的失败状态，每个意图仍只执行一次。相同恢复请求返回相同后续意图，不重复排队。

重试使用原意图的相同执行证据版本，并携带此前已生效写入和待批准提案的摘要。原生提示词要求先检查当前可访问的技能和记忆，再继续仍需要的工作；已有技能不重新创建，已存在记忆不重复添加，待批准提案不重复提交。当前库中的后续修改保持有效，不用旧快照覆盖。

HTTP 401/403、权限拒绝、输入预算耗尽，以及结果为 `unknown` 或存在未确认写入的尝试停止自动重试。进程被强制结束后留下的 `unknown` 需要核对执行结果，不能换一个 ID 当作从未执行。达到重试上限后保留所有记录并显示停止原因。后续成功复盘已覆盖相同或更新的证据版本时，旧重试被标记为 `superseded`。

本次仅自动恢复 observation 产生的后台复盘。`user_request`、`attended` 和带 request_id 的主动学习意图不进入自动重试；`hermes-refine`／`hermes-learn` 继续保留其原材料、focus、结果绑定和取消流程，见 [主动学习合同](./ACTIVE-LEARNING.zh-CN.md)。

## 查看与手动提前重试

`learning_status` 和 `scripts/bridge status` 的 `review_retries` 提供状态计数、最近队列项、下一次重试时间和失败尝试 ID。列表最多显示 20 项，并明确标记是否还有未显示项。通知和回执显示失败原因、预计重试时间、重试完成或达到上限；原失败回执保留。

```sh
scripts/bridge status --settings /absolute/path/bridge.json
scripts/bridge review-retry --settings /absolute/path/bridge.json --attempt FAILED_ATTEMPT_ID
scripts/bridge review-retry --settings /absolute/path/bridge.json --attempt FAILED_ATTEMPT_ID --apply
```

手动入口默认预览。`--apply` 将已经发现、仍在等待的可重试任务提前到当前时间，由同一个 owner 循环执行；不直接调用模型、不解除未确认结果的限制、不重置累计重试次数。`eligible: false` 表示该任务没有进入执行队列。

## 配置

只合并 `bridge.json` 中的 `learning.retries`，保留其他配置：

```json
{
  "learning": {
    "retries": {
      "enabled": true,
      "max_retries": 3,
      "initial_delay_seconds": 300,
      "max_delay_seconds": 3600
    }
  }
}
```

未配置时采用以上默认值。重试上限只能为 1–10 次，延迟必须是 1 秒至 1 天内的有限值，最大延迟不能小于初始延迟。配置在 worker 启动时读取；关闭重试不删除已有记录。

## 存储与回退

桥接新增 `review_retries` 表。原生日志保留原来的表和记录，将内部存储版本从 1 升到 2，并增加恢复父意图索引。Learning API 对外版本仍为 1，通过 `review_retry` capability 声明恢复支持。旧存储可原位加载，已有失败和成功记录不改写；串行 owner、已开始尝试恢复为 `unknown` 的规则继续有效。

存储版本 2 防止旧核心把新的后续意图当普通复盘执行。只支持旧存储版本 1 的核心会拒绝该日志，因此成功采用新存储后，回退需要支持版本 2 的核心。候选启动失败时沿用受管激活事务恢复提交前快照；提交后的恢复保留当前日志和新知识，不把旧快照覆盖回来。旧 v0.22.7 补丁保留历史合同，本次恢复能力在 v0.23.0 与 v0.23.1 的共用补丁上提供。

## 验证范围

回归覆盖真实 LearningService 的持久恢复意图、保留部分写入、待批准提案和失败收据、分页、作用域隔离、上限、较新证据覆盖，以及桥接 owner 的冷却、重启、手动预览和完成通知。隔离 fixture 不调用真实模型；原生合同、真实 provider、受管运行时和桌面可见性分别验收。复盘成功和内容结构检查不证明学习质量提升，也不提供文件写入的跨尝试事务保证。
