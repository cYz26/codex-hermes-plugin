# Hermes Learning for Codex

本分支 `codex/cloud-dev-self-evolution` 提供云端 Linux／SSH 开发机实现，见 [运行与升级](./docs/LINUX-GLOBAL-UPDATES.zh-CN.md) 和 [SSH 桌面通知](./docs/SSH-NOTIFICATIONS.zh-CN.md)。本地 macOS 的独立分支见 [本地分支](https://github.com/cYz26/codex-hermes-plugin/tree/codex/local-learning-improvements)。

Codex 执行任务，独立 Hermes 从执行证据学习，再向后续任务提供项目记忆和经过检查的通用技能。当前外围版本为 **0.6.1**，学习核心为官方 **Hermes v0.23.0**（`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`）加 Learning API v1 补丁。

0.6.1 整合云端学习与 SSH 通知修复：保留 0.6.0 企业正文学习、主动复盘和技能应用证据，同时使用带图标的新通知发送方、保留历史回执，并提供通知授权恢复入口。

0.6.0 增加[企业专用环境学习](./docs/ENTERPRISE-LEARNING.zh-CN.md)：操作人确认当前企业模型与专用部署后，普通内部任务正文默认参与学习，可复用技能在该企业知识库共享；保留凭据过滤、项目记忆归属和企业范围标记。

0.5.0 增加来源绑定的语义学习、高价值事件优先、技能应用证据和隔离脚本验证，并修复服务替换、更新恢复及 SSH 通知积压。安装、准入合同和验收边界见 [云端 0.5.0](./docs/CLOUD-0.5.0.zh-CN.md)；实施与各项实际验收状态见 [实施记录](./docs/IMPLEMENTATION-0.5.0.zh-CN.md)。

[实现整理](./docs/SELF-EVOLUTION.zh-CN.md) 说明执行与学习闭环、资料准入、版本发布、运行时更新和恢复；[流程学习](./docs/WORKFLOW-METADATA.zh-CN.md) 说明全局固定流程投影的边界。安装入口、当前部署说明和历史验收见 [文档索引](./docs/README.md)。

## 工作方式

- Codex hooks 使用真实 cwd 确定项目身份，将获准的执行证据写入持久事件箱；hooks 不调用模型。
- 独立监督进程托管 Hermes worker，复用原生 review、Memory、Skills 和 Curator；配置、学习数据和运行中的主应用分开。
- 项目记忆按工作区召回。通用候选通过来源、路径、内容摘要及共享准入检查后才发布，拒绝的新候选不会替换上一已发布版本。
- 新建全局配置默认学习固定动作／状态。企业专用部署可显式启用正文学习，其他部署沿用来源准入；两种模式均不声称提供通用脱敏服务。
- 模型、provider、API 模式和推理档位通过原生配置选择。既有认证由 Hermes 管理，不复制 Token，也不改变 Codex 的当前模型配置。

Linux／SSH 与本地 macOS 使用独立分支，共用便携学习核心；平台服务和桌面接收组件由对应分支提供。原版 0.3.2 的 macOS 部署及 v0.22.7 验收保留原版本与范围。

## 检查和使用

需要 Python 3.11 或更高版本。外围客户端只有标准库依赖；Hermes worker 需要验证过的原生运行时及固定补丁。

```sh
python3 -B scripts/check_release.py
scripts/bridge --help
scripts/bridge init --help
scripts/bridge auth-status --settings /ABS/pilot/bridge.json
scripts/bridge doctor --settings /ABS/pilot/bridge.json --json
scripts/bridge status --settings /ABS/pilot/bridge.json
scripts/bridge update status --settings /ABS/pilot/bridge.json
```

新建配置先使用 `init --dry-run` 核对固定源码、解释器、project、home 和 provider 参数。选择 `--capture-scope global` 时默认启用资料准入、固定流程投影和检查后的共享；已有配置保留既有策略。平台安装步骤见分支文档及 [安装与更新](./docs/INSTALL.zh-CN.md)。

更新器发现官方稳定版后，核对精确提交、补丁和原生／外围合同，再由监督进程暂停学习并验证候选。`updates.auto_adapt` 默认关闭；开启后，Codex 维护兼容材料仍要通过未被修改的验证器，重设计或不可逆迁移需要具体方案。回退保护当前事件、已交付回执和根账号认证。

学习通知使用独立回执和桌面窗口。通知投递不代表系统横幅已显示，技能加载不代表业务验证通过。实际模型探针、App hooks 与 A→B→C 跨聊天验收分开记录，不用历史 macOS 结果替代新平台验收。

## 验证与源码边界

`check_release.py` 核对包版本、补丁、保留证据、链接和凭据，并运行离线回归；不会调用模型、安装插件或修改运行中的 Hermes。原生测试通过固定上游源码中的 `scripts/run_tests.sh` 执行；真实模型探针是显式、独立验证 home 中的合成夹具。

```text
src/hermes_codex_learning/   hooks、持久桥接、MCP、资料准入、发布和更新恢复
hermes-plugin/              原生 Hermes 插件入口
hooks/ skills/              Codex hooks 与技能加载策略
patches/                    固定官方基线、Learning API 补丁及兼容清单
scripts/ probes/ tests/     客户端、服务入口、显式模型探针与离线回归
docs/ evidence/             实现说明、分支验证和历史证据
macos/ assets/              通知 App 源码与插件图标
```

完整 Hermes checkout、依赖环境、用户认证、学习数据及回退快照不随仓库发布。补丁来源与许可证见 [第三方声明](./THIRD_PARTY_NOTICES.md)，历史证据的实际范围见 [证据索引](./evidence/README.md)。
