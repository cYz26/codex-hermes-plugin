# Codex 全局 Hermes 学习

2026-11-07，外围版本 **0.2.2**。个人知识库共享通用技能，项目记忆按来源范围召回。0.2.0 首次部署全局模式，0.2.1 完成两项目的学习与召回。0.2.2 的追加验收见 [发布验收](./RELEASE-0.2.2.zh-CN.md)。

## 实现

- Codex 全局插件只安装一次。`capture_scope: global` 接受本机主聊天的真实 hooks。0.2.1 根据会话启动和用户输入的工作区绑定项目，并可从已持久化的生命周期事件恢复归属。Git 项目使用最近的 Git 根目录，无 Git 的聊天使用会话工作区。工具调用的目录保留在证据中，项目归属由聊天工作区决定。
- 仍然排除子 agent 和 App 生成的下一条消息建议。没有实际绝对 cwd 的事件不猜测来源；远程／云端运行不在这份本机 socket 部署的验收范围内。
- 同一个隐藏 Hermes home 保留原生学习库和单 worker，原生阈值、120 秒空闲补充、600 秒最小补充间隔、Max 模型配置不变。核心 14 文件的补丁 hash 未变。
- 通用技能可跨项目按相关性发现并显式读取。`context(cwd, query)` 的 query 使用轻量关键词匹配，中英文均支持，不调用模型；正文仍通过原生 `skill_load` 读取并返回版本和交付收据。
- 内置记忆按 Hermes 原生条目分隔符建立外围来源索引。升级前记忆属于原 pilot；已确认的原生 memory 写入关联该次复盘的项目。来源不明的外部修改、写后崩溃遗留的新条目、合并其他项目条目的改写不自动注入。外围不改写原生记忆或技能文件。
- 共享技能保留 `source_projects`，正文可能有原项目的例子；模型应检查适用性，不能把其他项目的路径或规则当作当前项目约束。来源标注与相关性召回不是多租户权限隔离，也不声称完成语义级隐私分类。
- 未传 cwd 的旧 MCP 客户端只能获取无项目记忆的发现视图，不使用插件安装目录猜测任务工程。原生外部 memory provider 当前返回聚合的无范围 recall；全局 Codex 视图暂不注入该聚合内容，原生 provider 学习与维护仍保留。
- macOS 学习通知带项目名，点击打开本地回执。通知与模型上下文分别记录消费。模型摘要按项目范围提供，完整版通过 MCP 读取。

## 配置与回退

私有配置是 `~/.hermes-codex-learning/pilot/bridge.json`。全局模式显式增加：

```json
{
  "capture_scope": "global",
  "excluded_projects": []
}
```

其他 home、project、source、python、profile_home 和网络配置保持原值。`project` 作为历史 pilot 身份和 launchd 工作目录，不再作为全局 allowlist。`excluded_projects` 接受绝对路径及其子目录，可用于退出个别工程。配置和代码不能替代 Codex 的 hook 信任。

新增实例可在 `scripts/bridge init` 加 `--capture-scope global`；默认仍为 project，避免既有配置升级后未经请求扩大采集。回退时恢复 `capture_scope: project` 并在 worker 空闲时重载这一个服务；新项目的旧证据保留，不删除原生数据或恢复过期 OAuth 凭据。

升级前快照：`~/.hermes-codex-learning/backups/before-global-20261107-202539`，包含工作区、配置、SQLite 一致性备份与学习文件，不包含根认证。Hermes App 的隐藏入口与登录复用继续使用原部署。

## 验证状态

0.2.1 的 **34 项离线回归**通过。新增用例验证跨目录工具调用保留原聊天的范围，以及没有可靠工作区和已排除工作区的聊天无法通过工具目录接收项目记忆。

0.2.0 的真实 App 预检发现，当前聊天执行另一工程的命令时，工具事件的 `cwd` 会变成命令目录，导致召回切换范围。该次调用没有传回外部项目记忆。0.2.1 修复后，实际在 `dev001` 执行命令的两个工具事件仍归属 `hermes-plugin1102`。官方状态在两个项目均显示 7 个 `trusted`，21 个安装运行文件与源码一致。[0.2.0 实测问题](../evidence/v0.22.7/global-cross-project-cwd-repro.json) · [0.2.1 实测复验](../evidence/v0.22.7/global-cross-project-021-scope-probe.json)

两个项目各有一条从 App 输入框启动的执行聊天。命令均以 0 退出，Hermes 分别通过 `idle_flush` 自动复盘，各写入一条专属记忆。发布后，两边的范围查询只返回自己的标签。[真实 App 主机回执](../evidence/v0.22.7/global-cross-project-021-training-host.json) · [自动复盘与记忆来源](../evidence/v0.22.7/global-cross-project-021-training.json)

两条召回聊天由 Codex 的 App 工具创建。`dev001` 直接从 hook 摘要取回自己的标签。`hermes-plugin1102` 的摘要截断了标签，Codex 自行通过 MCP 查询本项目的完整记忆。两个标签都与各自的原始训练证据匹配，两条聊天的实际记录中均未出现另一项目的标签。创建收据与终态均已关联到原生投影。[自动召回证据](../evidence/v0.22.7/global-cross-project-021-auto-recall.json)

0.2.1 已在 `hermes-plugin1102` 通过实际 MCP 读取由 `dev001` 原生复盘修订的共享技能。正文和参考文件的版本、哈希及 `body_delivered` 可关联到当前 App 回合和工具调用。两个执行聊天结束后读取的共享技能版本也通过了交付校验，其正文和参考文件均不含专属标签。[跨项目技能交付](../evidence/v0.22.7/global-cross-project-021-shared-skill.json)

[0.2.0 全局部署历史记录](../evidence/v0.22.7/global-rollout.json) · [当前验收摘要](../ACCEPTANCE.json)

共享库仍使用 Hermes 原生记忆容量。范围路由、关键词召回和通知均由程序处理。Curator 到期维护与原生用量对照记录在 [0.2.2 验收](./RELEASE-0.2.2.zh-CN.md)。

## 按需自动验收

在本工程的 Codex 主聊天发送 **“自动验收 Hermes 跨项目召回”**。Codex 读取 `ACCEPTANCE.json` 中的训练证据，使用 App 的 `list_projects` 定位两个真实本地项目，再用 `create_thread` 分别启动一条新的只读召回聊天。每条提示以同一验收标识开头，例如 `HER-022-RECALL-AUTO`，要求找回本项目的 `receipt_scope_label`。摘要不足时，允许 Codex 自行查询当前项目的学习记忆。

协调聊天拿到两个 App 聊天 ID 后运行下列命令。脚本从实际 App hooks、创建收据、模型上下文和原生投影核对结果。等待后台投影时保留原有聊天 ID，稍后用同一命令继续检查。

```sh
python3 -B scripts/check_cross_project_recall.py \
  --marker HER-022-RECALL-AUTO \
  --chat-a "$recall_a" --chat-b "$recall_b" \
  --wait-seconds 120 --out evidence/private/cross-project-recall.json
```

`recall_a` 对应 `hermes-plugin1102`，`recall_b` 对应 `dev001`。输出只保留测试标签的哈希和证据身份。退出码 0 表示通过，2 表示仍在等待，1 表示结果或证据不符合要求。完整重验时由 Codex 先在两项目启动新的执行聊天，观察 Hermes 自动写入新的项目记忆，再用 `--training` 指定那一轮的训练收据。脚本和历史证据的检查入口分别是 [召回验证](../scripts/check_cross_project_recall.py) 和 [发布检查](../scripts/check_release.py)。
