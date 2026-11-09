# 文档索引

当前实现为外围 **0.4.2** 与官方 **Hermes v0.23.0**＋Learning API v1 补丁。Linux／SSH 和本地 macOS 共用学习核心，以不同分支提供平台适配。真实验收记录按当时版本保留，不能把安装或离线测试改写成模型／App 验收。

## 本地 macOS 分支

[本地运行与验证](./LOCAL-OPERATIONS.zh-CN.md) 说明 launchd、原生认证、通知 scope 与 sent／read 回执，以及本次 Linux 宿主可执行的离线检查范围。Linux 服务和 SSH 接收器由 [云端分支](https://github.com/cYz26/codex-hermes-plugin/tree/codex/cloud-dev-self-evolution) 提供。

## 当前实现

| 文档 | 内容 |
| --- | --- |
| [自进化实现整理](./SELF-EVOLUTION.zh-CN.md) | 执行与学习闭环、资料准入、项目归属、通用发布、适配和恢复 |
| [全局流程学习](./WORKFLOW-METADATA.zh-CN.md) | 固定动作／状态投影、受限原生 review、版本凭证和验证 |
| [工程入口](../README.md) | 运行边界、诊断和离线发布检查 |
| [安装与更新](./INSTALL.zh-CN.md) | 原有 macOS 安装流程；0.3.2 的机器部署内容保留原范围 |
| [主动更新与适配](./UPDATES.zh-CN.md) | 原有更新策略、验证、切换和回退；增量合同见当前实现整理 |
| [兼容清单](../patches/compatibility.json) | 固定基线、原生测试集合与 [v0.23.0 补丁](../patches/v0.23.0/manifest.json) |
| [插件图标](./design/hermes-learning-icon/README.md) | 源稿与标识出处 |

## 历史实现与验收

| 文档 | 保留范围 |
| --- | --- |
| [整体方案](./ARCHITECTURE.zh-CN.md) | 历史原生能力、认证、数据、触发与验收方案 |
| [全局启用](./GLOBAL-ROLLOUT.zh-CN.md) | 原有宿主部署、信任和跨项目验证 |
| [学习 profile 与 Hermes App](./PROFILE-DESKTOP.zh-CN.md) | 后台独立 profile 的定位与历史诊断 |
| [真实 App 续验](./APP-VALIDATION.zh-CN.md) | 0.1.6 单项目闭环与当时的验证边界 |
| [0.2.2 验收](./RELEASE-0.2.2.zh-CN.md) | 当时的 Curator、通知、注入和用量对照 |
| [历史机器可读验收](../ACCEPTANCE.json) | v0.22.7／0.2.2 的记录 |
| [证据索引](../evidence/README.md) | 原版、补丁版、外围测试和模型证据 |
| [当前方案摘要](../HERMES-CODEX-PLAN.md) | 原有方案分工 |
| [Learning API 合同](../HERMES-CODEX-EXTENSION-REVIEW.md) | v1 接口、持久化和故障语义 |
| [历史实施](../IMPLEMENTATION.md) | 当时部署、测试和源码指针 |

## 研究档案

[最初研究](../HERMES-CODEX-RESEARCH.md)、[集成方式比较](../HERMES-CODEX-OPTIONS.md)、[原生能力差异](../HERMES-CODEX-NATIVE-PARITY.md) 与 [历史探针](../HERMES-CODEX-PROBE.json) 保留研究时的版本依据。

历史设计另见 [v0.22.5 方案](./archive/HERMES-CODEX-PLAN.v0.22.5.md)、[扩展合同](./archive/HERMES-CODEX-EXTENSION-REVIEW.v0.22.5.md)、[v0.22.6 实施](./archive/IMPLEMENTATION.v0.22.6.md) 与 [获批方案](./archive/APPROVED-PLAN.v0.22.6.md)。原始证据摘要不改版本；私有完整运行记录与回退快照不进入 Git。
