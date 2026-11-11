# SSH 桌面通知与只读明细（0.4.2）

0.5.0 接收器兼容此安装入口，增加 V2 游标与批量传输、未知投递处置、远端 Python 选择及 90 天明细保留；详见 [当前运行合同](./CLOUD-0.5.0.zh-CN.md)。已有通知 App 的图标随包保留，独立更新工具为 desktop/update_macos_icon.py。

开发机上的真实学习变更生成持久回执，Mac 上的接收器经已有 SSH host 配置补收通知。通知不写入 `additionalContext`、`systemMessage`、聊天正文或 MCP 返回，也不启动 Codex 回合或调用模型。当前没有经过验证的 SSH 纯 UI 聊天提示接口，`notices capabilities` 报告 `chat_ui: false`。

## 持久投递

`notifications.sqlite` 与原生学习数据分开，升级回退不会重放已确认通知。项目通知使用工作区 scope，通用通知在共享检查及版本发布后生成。技能按实体去重，Memory 变更按复盘前后实际条目计算；无新增成果静默，失败、部分生效和待处理候选分别表示。

队列默认 queued，接收器收到系统接受标记后确认 sent，用户打开明细后确认 read。sent 只表示系统接受；read 表示窗口打开，不推断用户阅读理解。consumer 独立保存状态，detail／ack 必须带匹配 scope。

`scripts/bridge notices poll/detail/ack` 只供 UI 组件经 SSH 使用，不注册为 MCP 工具。回执明细留在独立窗口中。

## 在运行 Codex Desktop 的 Mac 上安装

```sh
git clone --single-branch --branch codex/cloud-dev-self-evolution \
  https://github.com/cYz26/codex-hermes-plugin.git
cd codex-hermes-plugin
python3 desktop/install_macos.py --host YOUR_SSH_HOST \
  --bridge /ABS/installed-plugin/scripts/bridge \
  --settings /ABS/pilot/bridge.json --dry-run
```

替换 SSH host 与开发机上的两个绝对路径，核对 dry-run 输出后使用相同命令去掉 `--dry-run`。需要已有 SSH host 配置、Python 3 和 Xcode Command Line Tools。安装器在用户目录编译并签名通知 App，安装专用 LaunchAgent；首次在 macOS「系统设置 → 通知 → Hermes 学习」允许通知。

通知 App 使用[现有 Hermes Learning 图标](./design/hermes-learning-icon/README.md)。已有安装需要在 Mac 上从更新后的仓库重跑安装命令，沿用原 host、bridge 和 settings；安装器会打包图标并刷新应用注册信息。下一条真实学习通知可用于核对显示效果。

如果接收器已安装且只需更新通知 App，在 Mac 上运行 `python3 desktop/update_macos_icon.py --dry-run` 核对目标，再去掉 `--dry-run` 执行。迁移使用新的标识 `com.cy.hermes-learning-receipts`，先获得该发送方的通知授权，再等待运行中的通知 App 退出并替换。旧标识 `com.cy.hermes-learning-notifier` 的 App 保留在 `~/Applications/.hermes-learning-legacy/Hermes 学习.app` 并重新注册，历史通知继续通过它打开回执。

新发送方先保留在 `~/Applications/.hermes-learning-pending/Hermes 学习.app`，注册到 LaunchServices 后以前台应用启动授权。安装器核对候选路径、发送方标识、允许状态及系统错误；授权被拒、结果缺失或超时时不替换原 App，候选也会保留供系统设置定位。重新运行会从当前源码构建并替换受管候选，继续使用同一发送方标识。macOS 已记录拒绝时不会再次弹窗，需要在「系统设置 → 通知 → Hermes 学习」允许该发送方，再重新运行安装。发布后注册失败会尝试恢复原 App，并保留新候选。

这条迁移路径参考[本地 macOS 分支的 69d898c 提交](https://github.com/cYz26/codex-hermes-plugin/commit/69d898caf2b98fd31cf3ecb67bf25db0f07e6003)，移植通知相关行为到云／SSH 分支。两个安装入口共用实现；应用仍位于原接收器配置中的路径，无需改 host、settings 或 consumer。新旧 App 的回执格式保留 SSH 的 scope 与 `.sent`／`.read` 标记。规范的 App 文件权限为 `755/644`，接收器状态目录保持私有。

新通知仍显示默认图标时，在 Mac 上运行 `python3 desktop/diagnose_macos_icon.py`。诊断器从接收器配置定位实际 App，读取图标声明、ICNS 条目、签名有效性及签名标识是否与 `CFBundleIdentifier` 一致，并用 macOS AppKit 检查原生解码、默认注册路径及系统读取的 App 图标。报告保存到 `report/notification-icon-report.json`，系统图标预览保存为 `report/macos-app-icon.png`。诊断器不修改通知 App，不发送通知；系统接受回执也不能证明图标已经显示。

用户明确要求测试时，可运行 `python3 desktop/repair_and_test_macos_icon.py`，预览目标、安装带图标的通知 App 并核对原生解码，再发送测试 A（当前接收器的直接启动方式）和 B（LaunchServices 系统启动方式）。比较两条新通知的图标，区分资源更新与启动方式的影响。回执与报告保存在 `report/`，该目录不进入 Git；授权失败也会保存报告。测试记录按实际发送方归属，旧发送方或来源未知的记录另行保留。默认重复执行只读取当前发送方的既有回执，不自动重发不确定投递；明确需要新一轮测试时加 `--new-test`。工具不修改接收器或学习配置，系统接受标记仍需配合用户观察。

需要迁移后立即测试时，可运行 `python3 desktop/refresh_macos_notification.py`。它先安装新发送方，再读取签名和原生图标信息，发送一条明确标注的新发送方测试通知。安装结果、旧 App 保留路径和投递回执保存到 `report/notification-refresh-report.json`。既有测试回执不会自动重发；来自旧标识的测试记录另行保留。实际图标显示需要在 Mac 观察新通知，不由发送成功推断。

若安装报告中的 `authorization.allowed=false`、`authorization_status=1`，本次安装停在新版授权步骤。可双击 `desktop/recover_macos_notification.command`，或在终端运行 `python3 desktop/refresh_macos_notification.py --recover-authorization`。脚本先读取授权；仍被拒绝时，使用[通知设置的应用入口](https://neat.software/blog/swift-go-to-apps-notification-settings-on-macos)打开新标识对应的页面。开启「允许通知」后返回终端按回车，脚本重新读取一次系统结果，再决定是否继续迁移。重新检查仍被拒绝时保留原 App，不发送测试。报告记录生成时间、各次授权结果和失败后的实际发送方，不能以旧的 `before` 快照判断当前安装。

接收器每五秒查询通知，同一 scope 的连续结果合并显示。点击横幅打开独立、只读 AppKit 明细窗口，并显示对应范围。状态、回执和本地投递记录位于 `~/Library/Application Support/HermesLearningSSH/`；不修改 Codex 的 provider、模型或 SSH 配置。

断线后补收，系统已接受的记录不会重复发送。进程在系统确认前中断的投递保留为不确定状态，先核对记录再处理，不能盲目重复通知。

```sh
launchctl print gui/$(id -u)/com.cy.hermes-learning-ssh
cat ~/Library/Application\ Support/HermesLearningSSH/status.json
```

## 验收范围

接收器离线测试验证同工作区合并、跨 scope 分开和重连去重；当前结果见 [云端分支验证](../evidence/releases/cloud-0.4.2.json)。真实验收需要在 Mac 看见横幅、点击只读明细，并核对断线补收、重启去重、无变化静默和工作区隔离。

本次仓库整理没有重新安装 Mac 组件或重做桌面可见性验收。整理前的宿主报告另见 [历史报告](../ACCEPTANCE.linux-global.json)；Linux 后端测试、`notify-send`、发送回执或旧 macOS 截图不能替代新部署的桌面验收。
