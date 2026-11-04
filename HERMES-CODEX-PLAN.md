# Codex 执行、Hermes 原生学习：当前方案

2026-11-04，v0.22.7 已部署并完成本轮回归。运行说明见 [README](./README.md)，证据和剩余 App 验收见 [IMPLEMENTATION](./IMPLEMENTATION.md)。

## 基线与扩展边界

基于官方 **Hermes v0.22.7 / `b200f5bc60e31588ba2cb60e288029c4a54ba5fa`**，增加通用 `ctx.learning(version=1)`。交付形式是官方源码＋可重放扩展补丁＋外围插件，不维护独立完整发行版。核心补丁修改 4 个官方文件，新增 6 个通用学习模块及 4 个测试文件；不引入旧定制版代码、配置、记忆、技能或更新器。

Codex App 执行业务；外围插件负责真实 hooks、身份与覆盖缺口、持久队列、空闲补充、Unix socket、MCP、通知和读回。Hermes 核心拥有原生 review、memory/skill 写入保护、技能锁、Curator 以及最小执行收据。Codex 核心不改。

## 认证与数据

学习数据仍独占 `/Users/cy/.hermes-codex-learning/pilot`。通过原生 profile 入口 `/Users/cy/.hermes/profiles/codex-learning-pilot` 继承现有 Hermes App 的认证；目录入口指向独立学习 home，凭据不复制，刷新按原生锁和源感知回写执行。配置 `gateway.standalone: true`，根 gateway 不接管此 profile。

模型固定 `gpt-6.1-sol / openai-codex / codex_responses`，用户指定 `agent.reasoning_effort: max`。同模型 review 继承该配置，不读取 Codex App 的模型选择或认证文件。已有 Hermes 登录无需再次授权。

## 学习、反馈与维护

保留原生回合／工具阈值、review prompt、工具白名单、预算、写入来源和技能锁。明确终态后空闲 120 秒可补充未审阅证据，至少间隔 10 分钟，并发 1；长命令未结束不当作空闲，同一批证据不重复复盘。Curator 使用原生到期、空闲规则，Hermes 不重新执行业务任务。

journal 原子记录观察、计数、intent 和 receipt；原生转录以稳定 UID 投影。结果区分生效、暂存、拒绝和不明，未知工作不盲目重试。原生批量工具结果保留技能身份；旧 v1 收据可继续召回。owner 重启后读取持久化原生 usage。

用户提示来自真实结果：hook 的 `systemMessage`、空闲时的系统通知，以及下一次安全点补收。模型通过约 256 tokens 的增量上下文获知成果；技能正文按需读取。通知用程序模板，不为通知额外启动模型。加载、采用和验证有效分别记录。

## 验证与升级

本轮官方对照 328 项、带补丁 343 项、外围 9 项、原生检查 14 项通过。现有账号已完成真实模型复盘：1 条记忆、1 个创建后继续修订的技能；原 App 记忆／技能在验证时间窗内未变。已安装 MCP 能召回并读取真实技能，重启后 review usage 保持 6 次，读取未增加复盘调用。

真实模型输入明确标记为验收 fixture；真实 App hooks 信任、A→B 自动学习召回、提示样式及成本对照仍需单独验收。不会将本轮原生／stdio 测试冒充 App 证据。保留原生 token 分项，无法获取的使用／费用标为未知，不把缓存或双份账本重复累计。

Hermes 更新时先保留一致性快照，在官方稳定版候选中重放本项目补丁、回归后再切换。Codex 更新主要复核外围采集和提示适配。v1 保持 POSIX/in-process、单 home 单 owner；不承诺任意新版本自动兼容。
