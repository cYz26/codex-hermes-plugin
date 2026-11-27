# 安装与更新

需要 Python 3.11+、Codex 或 Claude Code，以及与 [兼容清单](../patches/compatibility.json) 匹配的 Hermes 源码。两个宿主插件共用同一学习服务。macOS 本地宿主使用本页的 launchd 流程；Linux／SSH 宿主使用 [Linux 运行与升级](./LINUX-GLOBAL-UPDATES.zh-CN.md)，桌面 Mac 只需按 [SSH 通知](./SSH-NOTIFICATIONS.zh-CN.md) 安装接收器。

现有 Linux 学习服务可使用 `scripts/bridge install --settings /ABS/pilot/bridge.json` 预览不可变安装包，再加 `--apply --start` 排空复盘并替换服务。继续复用原配置、认证和学习数据，详见 [云端安装合同](./CLOUD-0.5.0.zh-CN.md)。

## Codex 插件

在干净的发布版本检出根目录，使用目标桌面 App 对应的 Codex CLI：

```sh
codex plugin marketplace add "$PWD" --json
codex plugin add hermes-learning@hermes-learning-local --json
```

已有本地来源时，更新仓库后再次执行第二条。Codex 插件目录中的名称为 **Hermes Learning**，图标由[插件清单](../.codex-plugin/plugin.json)统一引用。双宿主版本把 Codex hooks 移到 `hooks/codex.json`；旧路径的批准不继承，需在 Codex 界面重新审阅并批准。新开聊天后用本页的回读命令确认。

## Claude Code 插件

已有 Hermes worker 时，按下文更新服务到同一构建，保留现有 `bridge.json`。在实际任务工作区开一个测试会话，用绝对路径加载插件；这不会全局安装：

```bash
claude --plugin-dir /absolute/path/to/codex-hermes-plugin
```

长期使用时，从干净的发布 checkout 或不可变安装包添加本地 marketplace，再安装插件：

```bash
claude plugin marketplace add /absolute/path/to/codex-hermes-plugin
```

```bash
claude plugin install hermes-learning@hermes-learning-local
```

新开会话，在 `/mcp` 确认 `plugin:hermes-learning:hermes_learning` 已连接。若使用非默认配置，确保启动 Claude Code 的环境与 worker 指向同一份 `HERMES_LEARNING_CONFIG`。不要再手动添加同一个 MCP 服务。

Claude Code 在 hook 中提供 `session_id`（对应 `chat` / `chat_id`）和 `prompt_id`（对应 `turn`）。插件 skills 可展开当前 `${CLAUDE_SESSION_ID}`。没有 `prompt_id` 的客户端仍可收到会话召回，但不归并回合正文。取消或一次性 `-p` 进程提前退出时可能缺少结束回执，插件不会读取 Claude 私有 transcript 来猜测结果。

项目记忆和 MCP 技能召回不需要额外投影。要让通过发布检查的技能出现在 Claude Code 原生技能列表中，在同一个 `bridge.json` 的 `projection` 对象中设置 `claude_skills_dir` 为 `~/.claude/skills` 的对应路径（若使用自定义配置目录，填该目录下的 `skills`）。重启 worker 后新开会话；检查 `status` 的 `native_projection.claude` 与 `live_claude_symlinks`。投影复用不可变快照，不覆盖同名用户技能；项目私有或未投影版本继续用 `skill_load`。

本地 CLI 2.1.330 已用于隔离验证。用 `claude plugin validate /absolute/path/to/codex-hermes-plugin` 检查清单，用 `learning_status` 在实际工作区核对采集和消费。CLI 通过不代表 Desktop 图形会话或线上模型复盘已验收。

## 新建 macOS 本地学习实例

从[兼容清单](../patches/compatibility.json)选定官方 commit。在独立检出中应用对应 Learning API v1 补丁；[v0.23.1 清单](../patches/v0.23.1/manifest.json)记录补丁和生效文件的校验值。从该检出用 Hermes 的 PM 创建依赖环境：

```sh
HERMES_HOME=/absolute/learning/pilot HERMES_RUNTIME_DIR=/absolute/learning/tools \
python3 -B -m pm.build_env --source . --out /absolute/learning/runtime --group dev --group test
```

回到插件仓库，用上述环境初始化一次：

```sh
/absolute/learning/runtime/bin/python scripts/bridge init \
  --source /absolute/hermes-source --python /absolute/learning/runtime/bin/python \
  --project /absolute/pilot-project --home /absolute/learning/pilot \
  --profile-home /absolute/existing-hermes-root/profiles/.codex-learning-pilot
```

初始化会核对补丁和实际源码，建立独立学习目录及后台 profile。默认只采集指定项目；要采集本机主聊天，在初始化时加 `--capture-scope global`。`profile_home` 保留 `root/profiles/entry` 的路径写法，以便原生认证回退；账号凭据仍由 Hermes 管理。指定其他配置时，用 `--settings` 或 `HERMES_LEARNING_CONFIG` 选择同一份 `bridge.json`。

从常驻的本机 Python 启动监督进程。它按 `bridge.json` 为 worker 选择 Hermes 的 Python。要绑定 Hermes App 的官方依赖环境，按[共用官方安装](./UPDATES.zh-CN.md#共用官方安装)验证并切换。

```sh
python3 -B scripts/bridge launchd --settings /absolute/learning/pilot/bridge.json --start
```

可选：运行 `scripts/bridge install-notifier` 安装 **Hermes 学习**（带文件夹的蓝色 Codex + Hermes 图标）和 **Hermes 全局共享**（带小地球的紫色 Codex + Hermes 图标）。两个发送方在系统设置中分别管理。待审批使用系统提示音，普通回执保持静音。交互详见[待审批通知](./PENDING-REVIEWS.zh-CN.md#mac-桌面入口)。

从旧地球图标升级时，全局发送方使用新的 macOS 通知标识。首次安装时为新版「Hermes 全局共享」允许通知，旧发送方保留用于打开历史回执。更新学习服务后再升级通知 App。

已有通知 App 时，重新运行此命令即可迁移。按 macOS 提示允许新版「Hermes 学习」发送通知。授权成功并等待运行中的通知 App 退出后，安装器会替换发送方，并在 `~/Applications/.hermes-learning-legacy/` 下保留和重新注册历史发送方，供历史通知打开回执。再次安装时，登记范围为当前发送方及该归档目录中仍存在的历史 App。

## 更新现有学习服务

继续使用原有 `bridge.json` 和学习目录。备份旧 plist 后，用常驻 Python 重新生成 LaunchAgent；有复盘正在执行时，等它结束再重新加载服务。

```sh
python3 -B scripts/bridge launchd --settings /absolute/learning/pilot/bridge.json --replace
launchctl bootout "gui/$(id -u)/com.cy.hermes-codex-learning.pilot"
```

等待 `launchctl print "gui/$(id -u)/com.cy.hermes-codex-learning.pilot"` 报找不到旧服务，再加载新 plist。`bootout` 返回后，macOS 可能仍在注销服务；此时立即 `bootstrap` 会失败。

```sh
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.cy.hermes-codex-learning.pilot.plist
```

## 安装后回读

```sh
codex plugin list --json
python3 -B scripts/check_codex_hooks.py --codex /absolute/path/to/codex
scripts/bridge status --settings /absolute/learning/pilot/bridge.json
scripts/bridge update status --settings /absolute/learning/pilot/bridge.json
```

插件应为已启用。若 hooks 检查退出码为 2，在 Codex 的插件界面审阅并信任对应 hooks；回读为 `trusted` 或 `managed` 后才算采集配置就绪。确认 worker `ready`、Learning API 为 1，并按部署选择 `official_shared` 或 `private`。这些回读不代替实际 Codex App 捕获与真实模型复盘验收。
