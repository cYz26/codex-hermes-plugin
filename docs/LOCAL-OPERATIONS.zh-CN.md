# 本地 macOS 学习与通知（0.4.2）

主分支同时提供本地 macOS 学习和云端 Linux／SSH 学习。本页说明本地 launchd 与通知；同一 checkout 的云端操作见 [Linux 运行](./LINUX-GLOBAL-UPDATES.zh-CN.md) 和 [SSH 接收器](./SSH-NOTIFICATIONS.zh-CN.md)。

## 本次改动

通用部分包括固定流程学习、检查后的通用发布、原生后台复盘的非交互保护、明确的工具终态、兼容适配和 worker 恢复。完整实现见 [自进化实现整理](./SELF-EVOLUTION.zh-CN.md)。工具输出文本或布尔退出码不能把未知结果变成完成；升级启动失败时，流程技能凭证随对应内容版本一起恢复。

本地通知回执增加 `id` 与 `scope`，只读窗口在正文前显示工作区范围。旧回执没有 scope 时显示“本机”，保持旧 JSON 的读取兼容。系统接受通知后写入 `.sent` 标记，成功打开明细窗口后写入 `.read` 标记；标记表示系统接受和窗口打开，不推断用户已阅读或任务已通过。

通知 App 继续使用独立 AppKit 窗口，消息与明细不写入聊天正文或 MCP 上下文。构建和签名由本地安装器完成，仓库不包含用户电脑的二进制、配置、学习资料或认证。

## 配置与运行

需要 macOS、Python 3.11 或更高版本、Xcode Command Line Tools，以及按固定补丁核对过的独立 Hermes 学习运行时。

```sh
python3 scripts/bridge init \
  --source /ABS/patched-hermes-source --python /ABS/runtime/bin/python \
  --project /ABS/project --home /ABS/pilot \
  --capture-scope global --dry-run
```

核对 home、项目、解释器、原生模型参数与认证方式后，使用相同参数去掉 `--dry-run` 新建配置。默认 OAuth 通过隐藏 profile 复用原生认证；具名 provider 使用显式模型与 API 参数。已有配置不重新初始化，升级保留已选择的策略。

```sh
scripts/bridge auth-status --settings /ABS/pilot/bridge.json
scripts/bridge launchd --settings /ABS/pilot/bridge.json
scripts/bridge install-notifier
```

`launchd` 写入用户 LaunchAgent，默认不启动；安装新服务时在 macOS 上增加 `--start`。替换已存在定义需要 `--replace`。升级运行中的服务按 [安装与更新](./INSTALL.zh-CN.md) 的注销、确认、启动流程处理，不在活跃回合中重启 Codex 或 Hermes 主应用。

通知安装后在 macOS「系统设置 → 通知 → Hermes 学习」确认权限。`install-notifier` 会编译并签名 App；这一步和真实横幅／点击验收需要在 Mac 上进行。

## 验证与回退

```sh
python3 -B scripts/check_release.py
scripts/bridge doctor --settings /ABS/pilot/bridge.json --json
scripts/bridge update status --settings /ABS/pilot/bridge.json
```

`doctor` 展示文件配置和当前运行策略。`status --cwd /ABS/project` 提供该项目的共享目录预览、记忆容量及待审数量。技能读取中的 `host_readiness` 检查 worker 环境。实际执行技能前，可在对应终端用 `skill-readiness` 核对同一版本声明的平台、命令和环境变量。凭据文件与 Hermes 依赖会列为待检查，命令不会读取凭据值或安装依赖。

```sh
scripts/bridge memory-preview --cwd /ABS/project
scripts/bridge memory-preview --cwd /ABS/project --proposals /ABS/compaction.json
scripts/bridge publication-preview --cwd /ABS/project
scripts/bridge skill-readiness --cwd /ABS/project --name SKILL_NAME --artifact-version ARTIFACT_VERSION
scripts/bridge review-metrics --cwd /ABS/project --date 2026-11-13 --timezone Asia/Shanghai \
  --acceptance-chat ACCEPTANCE_CHAT --audit-chat AUDIT_CHAT
```

记忆预览按项目列出条目和当前快照的 `revision`。压缩 JSON 包含 `revision` 和 `replacements`，后者以条目 ID 为键、缩短后的完整条目为值。预览显示原文、拟替换文本和预计释放的原生字符数。实际修订沿用原生记忆审批。

发布预览显示当前和开启严格共享时的目录、可读版本、项目来源及已有判定。日报指标从复盘回执统计各组的 API 次数、tokens、墙钟耗时和实际变更。用已知聊天 ID 标注验收与审查，其他组包含尚未标注的聊天。各指标同时给出采集覆盖次数。旧回执可能只能提供两条通知的间隔，其中可能包含 worker 中断的时间。

`effective_policy.executable_validation` 显示独立验证后端。Linux 使用 `bwrap`。macOS 使用系统 `sandbox-exec` 和 Apple 开发工具自带 Python，执行前探测文件、网络和子进程限制。脚本只运行受信的合成用例。macOS 有 CPU、输出文件和墙钟上限，另采样监控常驻内存。后端或探测不可用时，脚本验证保持待核对。

`scripts/bridge retention` 默认预览过期回合与产物。恢复演练使用停止写入后的隔离备份，核对原生库和派生目录能恢复，宿主回合的删除标记仍能阻止旧事件重放。需要保留清理前的宿主证据时，可用 SQLite 备份单独封存，并从备份中只读核对。服务更新的回退会保留正在使用的宿主事件箱。

原本地分支在 Linux 开发机执行离线 Python 回归、包版本／补丁、历史证据、文档链接和凭据扫描，保留结果见 [0.4.2 基线记录](../evidence/releases/local-0.4.2.json)。测试能够验证便携核心，不能验证 macOS 的 Swift 编译、系统通知显示和实际点击；这些项明确保留为待 Mac 验证。

原 [0.2.2 macOS 验收](./RELEASE-0.2.2.zh-CN.md) 与 [历史验收 JSON](../ACCEPTANCE.json) 保留当时版本，不作为当前主分支新 UI 的验收。切换失败恢复学习数据和派生索引，保留当前采集事件、已交付回执和根账号认证；不要用旧快照覆盖新学习结果。
