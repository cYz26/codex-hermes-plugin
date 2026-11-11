# Hermes Learning for Codex

主分支同时支持本地 macOS 学习和云端 Linux／SSH 学习，使用同一个 Codex 插件、学习核心和更新合同。当前外围版本为 **0.8.0**；固定原生版本及补丁见 [兼容清单](./patches/compatibility.json)，包含 **Hermes v0.23.0** 和 **v0.23.1**。

Codex 执行任务，独立 Hermes 从获准的执行证据学习，再向后续任务提供项目记忆和经过检查的通用技能。安装插件和启动学习服务应在实际执行 Codex 任务的宿主完成。

0.8.0 整合通知复盘简述、真实状态摘要和拒绝操作明细，以及[待审记忆的差异查看与明确审批](./docs/PENDING-REVIEWS.zh-CN.md)；合法单条与批量调用使用相同范围检查；自动复盘失败默认按 5／10／20 分钟冷却，最多重试三次，见 [复盘恢复](./docs/REVIEW-RETRIES.zh-CN.md)。通知简述复用同一次复盘的最终回复，修改明细保留在后面。

0.7.0 增加[主动复盘与资料学习](./docs/ACTIVE-LEARNING.zh-CN.md)：支持用户显式请求、指定学习重点、按资料分批提炼技能，以及按请求查询实际结果。

| 场景 | 学习进程 | 桌面通知 | 操作入口 |
| --- | --- | --- | --- |
| 本地 macOS | Mac 上的 launchd 用户服务 | 本机 Hermes 学习 App | [本地运行](./docs/LOCAL-OPERATIONS.zh-CN.md) |
| 云端 Linux／SSH | 开发机上的 systemd 用户服务 | Mac 上的 SSH 接收器与同一通知 App | [Linux 运行](./docs/LINUX-GLOBAL-UPDATES.zh-CN.md)、[SSH 通知](./docs/SSH-NOTIFICATIONS.zh-CN.md) |

同一台 Mac 可以运行本地学习和 SSH 接收器：本地学习使用 `com.cy.hermes-codex-learning.pilot`，SSH 接收器使用 `com.cy.hermes-learning-ssh`，后者的状态保存在独立的 `~/Library/Application Support/HermesLearningSSH/`。两条通知路径共用受管 App 和回执格式，安装由同一锁串行处理；学习数据、投递记录和工作区来源保持各自归属。

## 安装与运行

需要 Python 3.11 或更高版本。外围客户端只有标准库依赖；学习 worker 需要固定补丁和验证过的原生 Hermes 运行时。

```sh
git clone git@github.com:cYz26/codex-hermes-plugin.git
cd codex-hermes-plugin
python3 -B scripts/check_release.py
scripts/bridge --help
```

[安装与更新](./docs/INSTALL.zh-CN.md) 说明同一插件的安装和 macOS 本地服务。Linux 宿主使用 `scripts/bridge systemd`；macOS 本地宿主使用 `scripts/bridge launchd`。SSH 模式只在 Mac 安装接收器，由它查询开发机上的回执，详见 [SSH 桌面通知](./docs/SSH-NOTIFICATIONS.zh-CN.md)。

初始化先使用 `init --dry-run` 核对固定源码、解释器、project、home、provider 和模型参数。选择 `--capture-scope global` 时，新配置默认启用资料准入、固定流程投影和检查后的共享；已有配置保留已选择的策略。模型、provider、API 模式和推理档位通过原生配置选择，认证由 Hermes 管理。

```sh
scripts/bridge auth-status --settings /ABS/pilot/bridge.json
scripts/bridge doctor --settings /ABS/pilot/bridge.json --json
scripts/bridge status --settings /ABS/pilot/bridge.json
scripts/bridge update status --settings /ABS/pilot/bridge.json
```

## 学习与更新

新版核心整合来源绑定的语义学习、高价值事件优先、技能应用证据、隔离脚本验证和留存恢复。构建摘要排除 Python 生成的安装元数据；历史验收保留原版本与范围。

[企业专用环境学习](./docs/ENTERPRISE-LEARNING.zh-CN.md)由操作人确认当前企业模型与专用部署后启用：普通内部任务正文参与学习，通用技能在该企业知识库复用；项目归属、凭据过滤和显式禁学约束继续生效。

hooks 使用真实 cwd 确定项目身份，将获准的执行证据写入持久事件箱；hooks 不调用模型。独立监督进程托管 Hermes worker，复用原生 review、Memory、Skills 和 Curator。项目记忆按工作区召回，通用候选通过来源、资源、摘要及共享准入检查后才发布。

[实现整理](./docs/SELF-EVOLUTION.zh-CN.md) 说明共同的执行与学习闭环；[流程学习](./docs/WORKFLOW-METADATA.zh-CN.md) 说明固定流程投影的边界。新建全局配置默认学习固定动作／状态；企业专用部署可按已绑定策略学习正文，其他部署沿用显式来源准入；流程投影不提供通用脱敏服务。

更新器发现官方稳定版后，核对精确提交、补丁和原生／外围合同，再由监督进程暂停学习并验证候选。`updates.auto_adapt` 默认关闭；开启后，兼容维护仍须通过独立验证器，重设计和不可逆迁移需要具体方案。恢复保留当前事件、已交付回执、学习结果和根账号认证。原生版本适配见 [更新流程](./docs/UPDATES.zh-CN.md)。

复盘回执显示技能名称、操作、资源路径和具体拒绝原因；相同操作与原因按尝试次数合并。原始记录缺少名称时明确标注“名称未记录”。单条与批量技能调用使用相同的范围检查。

## 验证与源码边界

`check_release.py` 核对包版本、补丁、保留证据、文档链接和凭据，并运行离线回归；不会调用模型、安装插件或修改运行中的 Hermes。原生测试通过固定上游源码中的 `scripts/run_tests.sh` 执行。真实模型探针、App hooks、跨聊天应用和桌面显示分别验收。

本地和云端分支的历史证据保留原版本与范围，见 [文档索引](./docs/README.md) 和 [证据索引](./evidence/README.md)。通知投递不代表系统横幅已显示，技能交付不代表已应用；离线测试不能替代 macOS 编译、签名、授权和实际点击验收。

```text
src/hermes_codex_learning/   共用 hooks、学习桥接、MCP、资料准入、发布与更新
hermes-plugin/              原生 Hermes 插件入口
hooks/ skills/              Codex hooks 与技能加载策略
scripts/ probes/ tests/     客户端、平台服务、显式探针与离线回归
desktop/ macos/ assets/     SSH 接收器、共用通知 App 源码与图标
patches/ docs/ evidence/    固定兼容材料、操作说明与保留证据
```

完整 Hermes checkout、依赖环境、用户认证、学习数据和回退快照不随仓库发布。补丁来源与许可证见 [第三方声明](./THIRD_PARTY_NOTICES.md)。
