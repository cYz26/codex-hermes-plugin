# 学习 profile 与 Hermes App 的使用边界

首次核查日期：2026-11-04；隐藏入口验证基线：官方 Hermes v0.22.7＋Learning API v1，外围插件 0.1.3。当前外围已更新为 0.1.6，见[真实 App 续验](./APP-VALIDATION.zh-CN.md)。用户随后已亲自确认 Sessions 与 Bots 均不再显示原后台名称。

## 当前状态：已从列表中隐藏

按用户要求，后台入口已从 `~/.hermes/profiles/codex-learning-pilot` 改为 `~/.hermes/profiles/.codex-learning-pilot`。只改注册链接及 `bridge.json`、专用 LaunchAgent 的启动路径；物理数据仍位于 `~/.hermes-codex-learning/pilot`。没有修改或重启 Hermes App，也没有追加 Hermes 核心补丁。

当前原生枚举只接受有效的 profile ID，忽略点号开头的目录。后台仍通过显式 `HERMES_HOME` 保留 `root/profiles/entry` 路径，由原生机制找到原认证根目录；它不再是供 `hermes -p` 选择的名称。Bots 自身的 `hidden` 展示选项不能同时影响 App profile 列表，因此没有采用仅隐藏 Bot 的办法。

真实只读接口的前后结果为：

| 接口 | 隐藏前 | 隐藏后 |
| --- | --- | --- |
| App：`GET /api/profiles` | default、coding、codex-learning-pilot | default、coding |
| Bots：`profiles.list` | default、coding、codex-learning-pilot | default、coding |

学习服务恢复为 `idle`、错误为空，原生登录仍从同一根账号继承；313 个学习记忆／技能文件的 SHA-256 一致，已有复盘 usage 的 API 调用数仍为 6。原生临时凭据合同检查同时覆盖了隐藏枚举、OAuth 刷新回写、Learning API 和数据隔离。本次没有增加模型调用。

界面自动化读取超时，因此以上是 App 实际后端接口验收，不冒充屏幕验收。若已有界面尚未刷新，应等待列表轮询或刷新该视图。原生身份验证和后台执行范围不因此扩大。

原生规则依据见固定版本的 [profile 枚举实现](https://github.com/NousResearch/hermes-agent/blob/b200f5bc60e31588ba2cb60e288029c4a54ba5fa/hermes_cli/profiles.py)和[认证根目录推导](https://github.com/NousResearch/hermes-agent/blob/b200f5bc60e31588ba2cb60e288029c4a54ba5fa/hermes_constants.py)。升级 Hermes 时需重跑 [隐藏入口合同探针](../probes/check_hidden_profile.py)。验证摘要见 [hidden-profile.json](../evidence/v0.22.7/hidden-profile.json)。

## 这个后台作用域用来做什么

`codex-learning-pilot` 是后台学习服务的数据和配置身份，用来独立保存从 Codex 执行证据中学到的记忆、技能、会话与复盘记录，同时通过 Hermes 原生 profile 机制复用现有登录。它不是用户执行任务的入口；任务继续在 Codex 中进行，学习不需要用户切换到 Hermes App 或主动要求总结。

当前实现没有把“在 Hermes App 中进入这个 profile 聊天／作为 Bot 使用”纳入支持和验收。此前 App 中出现该名称，只能说明它发现了 profile 入口，不能证明 App 能正确路由到它。普通 Hermes profile 的用途不限于聊天；官方也区分了 profile 与 Bot 的展示和会话身份。[官方 profiles 文档](https://hermes-agent.nousresearch.com/docs/user-guide/profiles)

这不是说所有后台 profile 都不能被 App 支持，而是本项目当前部署尚未实现这项兼容。出现错误也不是期望的界面行为，应该作为已知集成缺口保留。

## 本次确认的原因

隐藏前的目录关系为：

```text
~/.hermes/profiles/codex-learning-pilot → ~/.hermes-codex-learning/pilot
```

有两处分别可复现的问题：

1. **解析后的目录身份丢失。** 保留 `root/profiles/name` 的启动路径足以让已验收的学习服务继承登录；但 App 启动的原生 gateway 使用解析后的外部目录作为 home，将其标识为 `default`。随后 `session.list(profile="codex-learning-pilot")` 在网关内部绑定该 home 时，被拒绝为 `profile_mismatch`。同一网关的 `session.list(profile="default")` 能成功返回。这是只读真实 RPC 复现，没有启动模型或创建聊天。
2. **Desktop 默认路由与 standalone 部署冲突。** 学习 profile 的 `gateway.standalone: true` 将它排除在主网关服务集合之外；当前 Desktop 的普通本机 profile 路由默认复用主后端。日志已出现 `primary-shared` 路由，主网关只服务原有 profiles；因此不能仅通过等待网关启动解决。

App 日志同时出现了错误的嵌套目录查找，例如 `~/.hermes-codex-learning/pilot/profiles/codex-learning-pilot`。截图中的 `Runtime not ready` 是 Desktop 未能完成后端就绪验证的提示，不足以单独判断学习服务状态。

对应官方固定版本源码：

- [目录与 profile 名称规则](https://github.com/NousResearch/hermes-agent/blob/b200f5bc60e31588ba2cb60e288029c4a54ba5fa/hermes_constants.py)
- [gateway 的 profile 身份和链接目录边界](https://github.com/NousResearch/hermes-agent/blob/b200f5bc60e31588ba2cb60e288029c4a54ba5fa/gateway/session_authorities.py)
- [profile 绑定的就绪检查](https://github.com/NousResearch/hermes-agent/blob/b200f5bc60e31588ba2cb60e288029c4a54ba5fa/gateway/session_readiness.py)
- [Desktop 本机后端路由规则](https://github.com/NousResearch/hermes-agent/blob/b200f5bc60e31588ba2cb60e288029c4a54ba5fa/apps/desktop/electron/connection-config.ts)

## 怎样查看学习状态和成果

在本工程目录运行：

```sh
scripts/bridge status
scripts/bridge auth-status
```

`status` 返回学习服务状态、错误及复盘快照；`auth-status` 只读取原生认证状态，不输出凭据。学习记忆和技能保存在独立 home 的 `memories/`、`skills/` 中。Codex 插件还提供 `learning_status`、`context`、`skill_load` 读取入口；实际 App 加载仍以已有验收边界为准。

本次核查时，学习服务为 `idle`、错误为空，原有真实复盘记录仍为完成；认证为已登录且复用根账号，没有 profile 私有认证文件。因此这次 App 切换失败不要求重新授权。

## 处理与恢复范围

第一次诊断完成了真实 RPC 复现和使用边界纠正，未改变运行配置。随后按用户明确要求应用隐藏：备份原注册关系、桥接配置、专用服务定义、三个本地数据库的一致性快照和学习文件 hash；仅短暂停启学习服务并移除旧注册链接，没有删除学习数据或复制认证。首次重载遇到 launchd 异步注销时间窗口，恢复原服务后，等待退出和锁释放再重试，最终服务正常。

本机恢复材料在 `~/.hermes-codex-learning/backups/hide-profile-20261104-231322`。恢复时只需在学习服务停止后还原注册链接、桥接配置及专用服务定义；登录仍交给现有 Hermes 管理，不恢复旧 token。恢复可见链接也会恢复之前的 App 误选问题。历史验收保留原路径，不将旧记录重写为隐藏后的结果。

后续若增加 App 查看入口，应显式设计面向后台学习的只读展示。若增加同 profile 的交互执行，则需一并解决真实目录身份、Desktop 路由和学习写入生命周期；不能仅删除 `standalone` 或改名为 `default` 来绕过检查。现有认证和数据隔离测试不替代这些回归。
