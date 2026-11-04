# Learning API v1：当前实现合同

2026-11-04，官方 Hermes v0.22.7 基线。研究期合同见 [archive](./docs/archive/HERMES-CODEX-EXTENSION-REVIEW.v0.22.5.md)。实际验收见 [IMPLEMENTATION](./IMPLEMENTATION.md)。

外围只通过 ctx.learning(version=1) 使用核心能力，不导入 Agent、DB、Provider 或私有 review。入口要求插件 settings.learning.enabled 为 true，绑定 manager 的 profile，拒绝未知 API 版本；v1 在 isolated host 下明确不支持。

| 操作 | 核心行为 |
| --- | --- |
| open(source) | host/project/chat 确定稳定 scope，executor 可选；返回 opaque handle，原生模型上下文按需初始化。 |
| observe(handle, observation, event_id) | 校验终态、去重、原子记录消息/计数/intent/receipt，再以稳定 UID 投影原生转录。 |
| run_review(handle, intent_id, attempt_id) | 运行 native unattended fork，返回真实完成状态、applied/staged/refused/uncertain 和 native usage。 |
| read(kind) | status 返回 intent/attempt/投影缺口；context 返回生效记忆与目录；skill 返回完整正文、资源版本与交付 token。 |
| record_usage(token, evidence) | 对确认正文交付的宿主事件去重，调用 native view/use；原生 ledger 是 best-effort telemetry。 |
| maintenance(handle, idle_flush) | 显式签发补充触发，或驱动原生 Curator 时机检查。 |
| cancel() | 请求原生协作取消；不表示写入已回滚。 |
| close(handle, event_id) | 真实 SessionEnd 去重收尾，有待学习任务则 deferred；无 handle 则释放 owner 并报告 provider drain。 |

核心新增 agent/learning_*；原有文件只接入 ctx、共享 skill nudge 判断、partial review 消息保存和 host 不支持表。原生 prompt、写入工具、锁和 Curator 决策没有复制到外围。

Journal schema v1 拒绝未知版本。Journal、native state.db、记忆/技能文件、远端 provider 不是一个原子事务；投影中丢进程为 uncertain，已开始复盘丢结果为 unknown。未知工作不盲目重跑。暂存成果不发布，不改账本伪造活跃度。

外围负责真实身份、覆盖缺口、脱敏、持久 outbox、attempt 映射、空闲判断、通知独立消费、技能交付和 launchd。缺失 turn/tool ID 不猜测工具次数；内容 hash 只辅助确认具有相同来源身份的重投/修订。

本轮增加原生 profile 认证继承与数据隔离回归，批量工具身份的 v1 兼容读取，以及 owner 重启后的原生 usage 恢复；外围插件为 0.1.3。认证复用不增加 Codex 专属核心接口，凭据刷新仍由 Hermes 原生负责。
