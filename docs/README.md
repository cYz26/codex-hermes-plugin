# 文档索引

主分支的外围版本为 **0.8.0**，同时包含本地 macOS 和云端 Linux／SSH 适配。固定原生版本及补丁以 [兼容清单](../patches/compatibility.json) 为准。真实验收记录保留当时的版本和范围。

## 平台入口

| 文档 | 内容 |
| --- | --- |
| [安装与更新](./INSTALL.zh-CN.md) | 同一 Codex 插件的安装、宿主选择和 macOS 本地服务 |
| [本地 macOS 运行](./LOCAL-OPERATIONS.zh-CN.md) | launchd、原生认证、通知 scope 和 sent／read 回执 |
| [Linux 运行与升级](./LINUX-GLOBAL-UPDATES.zh-CN.md) | 初始化、systemd、诊断、自动适配与回退 |
| [SSH 桌面通知](./SSH-NOTIFICATIONS.zh-CN.md) | Mac 接收器、持久投递、图标诊断与授权恢复 |
| [云端组件与分工](./CODEX-CLOUD-SELF-EVOLUTION.zh-CN.md) | 学习宿主、桌面接收器和资料边界 |

## 共用实现

| 文档 | 内容 |
| --- | --- |
| [工程入口](../README.md) | 两种场景、配置和离线检查 |
| [自进化实现整理](./SELF-EVOLUTION.zh-CN.md) | 执行与学习闭环、资料准入、项目归属、发布和恢复 |
| [待审记忆处理与验证](./PENDING-REVIEWS.zh-CN.md) | 项目范围内的差异、批准／丢弃、重复清理及 Mac 验收入口 |
| [复盘恢复与重试](./REVIEW-RETRIES.zh-CN.md) | 自动复盘冷却、失败回执保留、手动提前与 schema 2 回退边界 |
| [主动复盘与资料学习](./ACTIVE-LEARNING.zh-CN.md) | 显式请求、资料分批学习、查询与恢复 |
| [企业专用环境学习](./ENTERPRISE-LEARNING.zh-CN.md) | 企业正文学习、配置绑定与企业内共享 |
| [全局流程学习](./WORKFLOW-METADATA.zh-CN.md) | 固定流程投影、受限 review 和版本凭证 |
| [更新流程](./UPDATES.zh-CN.md) | 候选验证、v0.23.1 适配、切换与回退 |
| [兼容清单](../patches/compatibility.json) | 固定基线、补丁和原生测试集合 |
| [插件图标](./design/hermes-learning-icon/README.md) | 源稿与标识出处 |

## 历史分支与验收

| 文档 | 保留范围 |
| --- | --- |
| [云端 0.5.0 合同](./CLOUD-0.5.0.zh-CN.md) | 语义准入、主动复盘、通知与留存的引入版本 |
| [0.5.0 实施与验收](./IMPLEMENTATION-0.5.0.zh-CN.md) | 原云端实现与分别记录的验收范围 |
| [云端 0.6.0 记录](../evidence/releases/cloud-0.6.0.json) | 企业正文学习的引入与验证 |
| [本地 0.4.2 基线](../evidence/releases/local-0.4.2.json) | 原本地分支的离线检查，macOS 实机项目单独记录 |
| [云端 0.4.2 基线](../evidence/releases/cloud-0.4.2.json) | 原云端分支的离线检查与补丁验证 |
| [历史全局宿主报告](../ACCEPTANCE.linux-global.json) | 整理前 0.4.2／0.4.1 的验证摘要 |
| [历史 Linux 试点](./LINUX-OPERATIONS.zh-CN.md) | 0.2.0 的运行记录及 [当时验收](../ACCEPTANCE.linux.json) |
| [整体方案](./ARCHITECTURE.zh-CN.md) | 历史原生能力、认证、触发与验收方案 |
| [全局启用](./GLOBAL-ROLLOUT.zh-CN.md) | 原有宿主部署、信任和跨项目验证 |
| [学习 profile 与 Hermes App](./PROFILE-DESKTOP.zh-CN.md) | 后台独立 profile 的定位与历史诊断 |
| [真实 App 续验](./APP-VALIDATION.zh-CN.md) | 0.1.6 单项目闭环与当时的验证范围 |
| [0.2.2 验收](./RELEASE-0.2.2.zh-CN.md) | 当时的 Curator、通知、注入和用量对照 |
| [历史机器可读验收](../ACCEPTANCE.json) | v0.22.7／0.2.2 的记录 |
| [证据索引](../evidence/README.md) | 原版、补丁版、外围测试与模型证据 |
| [历史方案摘要](../HERMES-CODEX-PLAN.md) | 原有方案分工 |
| [Learning API 合同](../HERMES-CODEX-EXTENSION-REVIEW.md) | v1 接口、持久化和故障语义 |
| [历史实施](../IMPLEMENTATION.md) | 当时部署、测试和源码指针 |

## 研究档案

[最初研究](../HERMES-CODEX-RESEARCH.md)、[集成方式比较](../HERMES-CODEX-OPTIONS.md)、[原生能力差异](../HERMES-CODEX-NATIVE-PARITY.md) 与 [历史探针](../HERMES-CODEX-PROBE.json) 保留研究时的版本依据。

历史设计另见 [v0.22.5 方案](./archive/HERMES-CODEX-PLAN.v0.22.5.md)、[扩展合同](./archive/HERMES-CODEX-EXTENSION-REVIEW.v0.22.5.md)、[v0.22.6 实施](./archive/IMPLEMENTATION.v0.22.6.md) 与 [获批方案](./archive/APPROVED-PLAN.v0.22.6.md)。原始证据不改版本；私有运行记录与回退快照不进入 Git。
