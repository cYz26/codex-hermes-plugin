# 云端开发机中的 Codex 与 Hermes 协作

`codex/cloud-dev-self-evolution` 提供 Linux／SSH 开发机实现。Codex Desktop 是入口，任务执行、hooks、学习桥接与原生 Hermes worker 位于同一开发机用户环境；用户电脑只接收独立桌面通知。外围为 0.4.2，核心固定为 Hermes v0.23.0 加 Learning API v1。

## 组件与归属

| 组件 | 运行位置 | 职责与实现 |
| --- | --- | --- |
| Codex 主任务 | SSH 开发机 | 执行用户任务，使用原有 provider、模型、工具和策略 |
| hooks 客户端 | SSH 开发机 | `capture.py`、`normalize.py`：资料准入、持久事件箱、明确的工具终态 |
| 学习 bridge／worker | SSH 开发机 | `daemon.py`、原生插件：同一个 worker 串行复盘、项目来源与固定流程学习 |
| 监督服务 | SSH 开发机 | `supervisor.py`、`systemd.py`：恢复、候选验证、暂停启动和切换 |
| 通用技能发布 | SSH 开发机 | `knowledge.py`、`policy.py`：来源、资源与内容版本检查，按工作区召回 |
| 兼容维护 | SSH 开发机 | `updates.py`、`adaptation.py`：固定目标、受限材料改动与独立验证 |
| SSH 通知接收器 | 运行 Desktop 的 Mac | `desktop/receive.py`：断线补收、同 scope 合并、sent／read 去重 |
| 通知明细窗口 | Mac | `macos/HermesLearningNotifier.swift`：独立、只读的 AppKit 窗口 |

学习实例使用独立 home、数据和状态文件，认证由原生 provider 管理。既有主应用保留其配置和运行中的源码视图。具名 provider 的会话亲和头由原生机制生成，不能用静态会话身份替代。

## 数据与模型边界

新建全局配置默认开启固定流程投影：未准入的业务正文只留下固定动作／状态，原任务内容与输出不会转入这条复盘路径。完整业务学习需要已配置的评估程序；固定流程投影不是通用脱敏器。项目记忆按工作区返回，通用候选在发布检查通过后才可跨项目加载。

模型调用留在原生 Hermes 学习 worker。hooks、通知查询和独立明细窗口不调用模型；通知内容不进入聊天上下文。真实宿主的字段缺口通过有界、当前回合且已验证身份的 transcript 补充，流程模式关闭任务正文提取，不扫描旧聊天历史。

## 平台选择与操作入口

本分支使用 systemd 用户服务托管 Linux 学习监督进程。Mac 接收器通过用户已有 SSH host 配置查询通知，不修改 Codex 的 provider、模型或 SSH 配置。无需把 SSH 开发机伪装成本地 macOS，也无需启动另一个企业问答 Agent 来承担主任务。

通用闭环和更新合同见 [自进化实现整理](./SELF-EVOLUTION.zh-CN.md)；实际初始化、systemd、适配和回退见 [Linux 运行与升级](./LINUX-GLOBAL-UPDATES.zh-CN.md)；Mac 组件见 [SSH 桌面通知](./SSH-NOTIFICATIONS.zh-CN.md)。本地 macOS 改进使用 [独立本地分支](https://github.com/cYz26/codex-hermes-plugin/tree/codex/local-learning-improvements)。

## 验证范围

[当前分支验证](../evidence/releases/cloud-0.4.2.json) 记录本次可重复的离线检查。整理前的模型、App A→B→C、桌面可见性和适配演练保留为 [历史宿主报告](../ACCEPTANCE.linux-global.json)，原始报告的摘要用于来源关联，机器路径和聊天标识省略；本次提交不重新执行这些在线验收。

新宿主部署应分别验证真实 hooks、原生自动复盘、后续聊天加载／应用对应版本、修订后的再次验证，以及桌面断连补收与点击。安装成功、模型合成探针、MCP 版本交付或系统接受通知，都不能单独证明完整协作闭环。
