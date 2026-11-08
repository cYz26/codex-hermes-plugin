# 文档索引

当前学习运行时为官方 Hermes **v0.23.0**＋Learning API v1 补丁、外围插件 **0.3.2**、模型 **gpt-6.1-sol / Max**。学习 worker 共用官方安装的 Python 依赖环境，复用已有 Hermes 登录，学习数据独立。旧版 App/provider 验收保留原版本。

| 文档 | 用途 |
| --- | --- |
| [安装与更新](./INSTALL.zh-CN.md) | Codex 插件、学习运行时、macOS 服务和回读 |
| [主动更新与适配](./UPDATES.zh-CN.md) | 自动检查、候选验证、切换与回退；自进化重设计先商定方案 |
| [插件图标](./design/hermes-learning-icon/README.md) | 学习回路图标、源稿和标识出处 |
| [全局启用](./GLOBAL-ROLLOUT.zh-CN.md) | 全局采集、项目记忆、共享技能、部署与宿主信任状态 |
| [整体方案](./ARCHITECTURE.zh-CN.md) | 架构、原生能力边界、认证、数据、触发、反馈、tokens、验收和升级 |
| [学习 profile 与 Hermes App](./PROFILE-DESKTOP.zh-CN.md) | 后台专用定位、App 切换报错的复现和当前查看方式 |
| [真实 Codex App 续验](./APP-VALIDATION.zh-CN.md) | 0.1.6 单项目闭环、信任恢复、真实版本交付、通知与边界 |
| [0.2.2 验收](./RELEASE-0.2.2.zh-CN.md) | Curator 到期、通知点击、注入上限、两组同任务用量和复验命令 |
| [工程入口](../README.md) | 安装、运行、目录结构和可移植检查命令 |
| [当前方案摘要](../HERMES-CODEX-PLAN.md) | 当前实现分工与策略 |
| [Learning API 合同](../HERMES-CODEX-EXTENSION-REVIEW.md) | v1 接口、持久化、故障语义和支持边界 |
| [实施与验收](../IMPLEMENTATION.md) | 实际部署、测试结果、真实模型证据和剩余 App 验收 |
| [机器可读结果](../ACCEPTANCE.json) | 可提交的验收摘要；本机详细状态另行保留 |
| [证据索引](../evidence/README.md) | 原版、补丁版、外围、红绿测试和原生模型证据 |
| [兼容清单](../patches/compatibility.json) | 官方基线、补丁和验证要求；包含 [v0.23.0](../patches/v0.23.0/manifest.json) 与 [v0.22.7](../patches/manifest.json) |

## 历史研究

以下文档保留研究过程及当时的源码依据，包含 v0.22.5 或 v0.22.6 的结论；不替代当前实现和验收记录。

- [最初研究](../HERMES-CODEX-RESEARCH.md)
- [集成方式比较](../HERMES-CODEX-OPTIONS.md)
- [原生能力差异分析](../HERMES-CODEX-NATIVE-PARITY.md)
- [历史探针原文件](../HERMES-CODEX-PROBE.json)：字节和 SHA-256 保持不变，仅证明探针当时覆盖的路径。
- [v0.22.5 研究方案](./archive/HERMES-CODEX-PLAN.v0.22.5.md)
- [v0.22.5 扩展合同研究](./archive/HERMES-CODEX-EXTENSION-REVIEW.v0.22.5.md)
- [v0.22.6 实施记录](./archive/IMPLEMENTATION.v0.22.6.md)
- [原始获批方案](./archive/APPROVED-PLAN.before-max.md)与 [v0.22.6 / Max 方案](./archive/APPROVED-PLAN.v0.22.6.md)

原始测试记录按版本保留。私有运行回执和升级快照不进入 Git；不会将其删除后用摘要冒充原始记录。
