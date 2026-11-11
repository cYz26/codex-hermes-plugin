# 安装与更新

需要 Python 3.11+、Codex CLI 或带 Codex 的桌面 App，以及与 [兼容清单](../patches/compatibility.json) 匹配的 Hermes 源码。两种场景使用同一插件：在实际执行 Codex 任务的宿主安装插件与学习服务。macOS 本地宿主使用本页的 launchd 流程；Linux／SSH 宿主使用 [Linux 运行与升级](./LINUX-GLOBAL-UPDATES.zh-CN.md)，桌面 Mac 只需按 [SSH 通知](./SSH-NOTIFICATIONS.zh-CN.md) 安装接收器。

现有 Linux 学习服务可使用 `scripts/bridge install --settings /ABS/pilot/bridge.json` 预览不可变安装包，再加 `--apply --start` 排空复盘并替换服务。继续复用原配置、认证和学习数据，详见 [云端安装合同](./CLOUD-0.5.0.zh-CN.md)。

## Codex 插件

在干净的发布版本检出根目录，使用目标桌面 App 对应的 Codex CLI：

```sh
codex plugin marketplace add "$PWD" --json
codex plugin add hermes-learning@hermes-learning-local --json
```

已有本地来源时，更新仓库后只需再次执行第二条。Codex 插件目录中的名称为 **Hermes Learning**，图标由[插件清单](../.codex-plugin/plugin.json)统一引用。新开聊天加载更新后的插件。

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

可选：运行 `scripts/bridge install-notifier` 安装 **Hermes 学习** 通知 App。安装器使用[现有插件图标](./design/hermes-learning-icon/README.md)，并采用新的发送方标识 `com.cy.hermes-learning-receipts` 刷新应用注册信息。新通知的实际图标显示仍需在 Mac 上核对。

已有通知 App 时，重新运行此命令即可迁移。首次迁移按 macOS 提示允许「Hermes 学习」发送通知；授权成功并等待运行中的通知 App 退出后才替换。旧发送方保留在 `~/Applications/.hermes-learning-legacy/Hermes 学习.app` 并重新注册，供历史通知打开回执。

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
