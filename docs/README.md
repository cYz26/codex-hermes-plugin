# 文档索引

当前目标为官方 Hermes **v0.22.7**＋Learning API v1 补丁、外围插件 **0.1.3**、模型 **gpt-6.1-sol / Max**。学习服务复用已有 Hermes 登录，学习数据独立。

| 文档 | 用途 |
| --- | --- |
| [整体方案](./ARCHITECTURE.zh-CN.md) | 架构、原生能力边界、认证、数据、触发、反馈、tokens、验收和升级 |
| [工程入口](../README.md) | 安装、运行、目录结构和可移植检查命令 |
| [当前方案摘要](../HERMES-CODEX-PLAN.md) | 当前实现分工与策略 |
| [Learning API 合同](../HERMES-CODEX-EXTENSION-REVIEW.md) | v1 接口、持久化、故障语义和支持边界 |
| [实施与验收](../IMPLEMENTATION.md) | 实际部署、测试结果、真实模型证据和剩余 App 验收 |
| [机器可读结果](../ACCEPTANCE.json) | 可提交的验收摘要；本机详细状态另行保留 |
| [证据索引](../evidence/README.md) | 原版、补丁版、外围、红绿测试和原生模型证据 |
| [补丁清单](../patches/manifest.json) | 官方基线、补丁 SHA-256 和 14 个受影响文件的内容摘要 |

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
