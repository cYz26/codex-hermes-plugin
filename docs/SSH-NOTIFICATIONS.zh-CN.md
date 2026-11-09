# SSH 桌面通知与只读明细（0.4.2）

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

接收器每五秒查询通知，同一 scope 的连续结果合并显示。点击横幅打开独立、只读 AppKit 明细窗口，并显示对应范围。状态、回执和本地投递记录位于 `~/Library/Application Support/HermesLearningSSH/`；不修改 Codex 的 provider、模型或 SSH 配置。

断线后补收，系统已接受的记录不会重复发送。进程在系统确认前中断的投递保留为不确定状态，先核对记录再处理，不能盲目重复通知。

```sh
launchctl print gui/$(id -u)/com.cy.hermes-learning-ssh
cat ~/Library/Application\ Support/HermesLearningSSH/status.json
```

## 验收范围

接收器离线测试验证同工作区合并、跨 scope 分开和重连去重；当前结果见 [云端分支验证](../evidence/releases/cloud-0.4.2.json)。真实验收需要在 Mac 看见横幅、点击只读明细，并核对断线补收、重启去重、无变化静默和工作区隔离。

本次仓库整理没有重新安装 Mac 组件或重做桌面可见性验收。整理前的宿主报告另见 [历史报告](../ACCEPTANCE.linux-global.json)；Linux 后端测试、`notify-send`、发送回执或旧 macOS 截图不能替代新部署的桌面验收。
