# 云端 0.5.0：安装、学习与验收

本版本面向单个 Linux／SSH 开发机及其 Mac Desktop 接收器，独立于 main 发布。Codex 执行任务，Hermes 从获准证据后台学习。项目记忆、技能正文交付、实际应用及任务验证分别记录；服务可达不代表整条闭环已验收。

## 安装与构建一致性

使用 Python 3.11+ 和匹配当前补丁清单的 Hermes v0.23.0。安装器创建不可变副本，保留原配置、模型和账号认证。

```sh
python3 -B scripts/check_release.py
python3 -B scripts/bridge install --settings /ABS/pilot/bridge.json --dry-run --start
python3 -B scripts/bridge install --settings /ABS/pilot/bridge.json --apply --start
```

已运行服务先完成当前复盘，再切换；失败恢复旧定义与绑定。安装输出中的 bundle 是后续使用的固定目录。Codex 插件仍通过正常 marketplace 安装该目录；新包按宿主规则检查 hooks 信任，新聊天加载更新后的 MCP。

配置选择顺序：显式 --settings、HERMES_LEARNING_CONFIG、用户安装指针、旧默认路径。安装指针只保存配置和包的位置，不修改 Shell。doctor 比较客户端与实际 worker 的内容摘要，版本号相同但源码不同会报告差异。

```sh
python3 -B scripts/bridge doctor --settings /ABS/pilot/bridge.json --json
python3 -B scripts/bridge update status --settings /ABS/pilot/bridge.json
```

## 正文准入与主动学习

没有获准正文时继续固定流程投影。内置评估器只处理来源清单中已核对的公开／合成片段，不承担通用资料分类或脱敏。公开来源使用匿名可读取、固定到具体提交的 raw GitHub URL；未被来源覆盖的聊天内容不会跟随目录一起放行。

```sh
scripts/bridge sources --manifest /ABS/policy/sources.json --public-url https://raw.githubusercontent.com/OWNER/REPO/COMMIT/PATH
scripts/bridge sources --manifest /ABS/policy/sources.json --public-url https://raw.githubusercontent.com/OWNER/REPO/COMMIT/PATH --apply
scripts/bridge learning-config --settings /ABS/pilot/bridge.json --source-manifest /ABS/policy/sources.json
scripts/bridge learning-config --settings /ABS/pilot/bridge.json --source-manifest /ABS/policy/sources.json --apply
```

COMMIT 必须是 40 位实际提交。合成验收资料可用 --synthetic /ABS/fixture.txt 注册；这不是为真实业务资料变更分类。配置写入后，使用 install --apply --start 排空并重新加载服务。

现有 assessment_command 保持兼容。V2 响应增加 schema_version=2、payload、input_hash、output_hash、policy_version；摘要使用桥接的规范 JSON digest。替代正文必须保留宿主身份，不能注入 bridge_* 字段。共享用途仍需 public 与 sharing_allowed；未知或校验失败继续投影流程。

高价值纠正、同一测试的失败后恢复、技能验证失败优先请求复盘。同一证据不重复请求；普通任务保留 120 秒空闲和 600 秒补充间隔，等待过久的普通任务获得调度机会。失败与完成均保留为执行事实，不推断修复因果。

## 技能验证与应用证据

技能正文检查适用范围、限制和验证说明，结果标记为 content_contract；它不证明业务效果。完全相同的规范正文与资源合并为别名，语义近似但内容不同的技能不自动合并。

脚本技能需声明 validation_suite 与 validation_entry。首个独立套件 normalize-headers-v1 验证从 stdin 读取字符串数组、去除首尾空白并执行 NFKC、向 stdout 输出数组。套件位于 fixtures/skill-validation；候选不能修改独立验证器。

验证使用 Linux bubblewrap、无网络、只读候选与运行时、临时可写目录，限制单次 60 秒、1 GiB 内存和输出大小。无法隔离或缺少独立套件时保持 pending；不会降级成直接在真实项目中执行。

```sh
scripts/bridge quality --settings /ABS/pilot/bridge.json --name learned-TOPIC
```

正文交付由 hooks 自动确认。Codex 在确实应用技能后可调用 record_application，提交交付标识与宿主事件引用，不提交任意聊天正文。application=self_reported 与 validation=host_test_passed 分开保存，effectiveness 始终不从单次测试推断。失败证据暂停售用对应版本，保留上一个可用发布。

## 通知、留存与维护

Mac 安装：

```sh
python3 desktop/install_macos.py --host EXISTING_SSH_ALIAS --bridge /ABS/BUNDLE/scripts/bridge --settings /ABS/pilot/bridge.json --remote-python /usr/bin/python3 --dry-run
```

核对后去掉 --dry-run。接收器协商 V2 协议，批量接收完整回执，持久化后才推进游标；未知投递不阻塞后续页。只有 .sent／.read 标记才确认 sent／read。明确核对未知批次后，可以用 receive.py 的 --resolve 与 --resolution retry|abandon 处置，默认不盲目重复发送。

正文保留 30 天、已确认通知明细保留 90 天。有效知识、来源摘要、去重标识和未完成／unknown 记录保留。清理通过原生维护入口与桥接各自的数据接口执行。

```sh
scripts/bridge retention --settings /ABS/pilot/bridge.json
scripts/bridge retention --settings /ABS/pilot/bridge.json --apply
scripts/bridge migration --help
```

migration 默认预览，--apply 才执行。seal 要求每个冲突与遗漏都有对应理由，verify 使用显式提供且已有 pytest 的测试解释器。site 适配不得替换独立测试运行器；候选源码的额外未跟踪文件会使验证失效。

## 验收

离线发布检查与固定原生合同由 scripts/verify_native.py 执行；真实 provider、App A→B→C、Mac 横幅／点击和 24 小时观察分别保留证据。历史报告不重新标记为 0.5.0 验收。

先用公开／合成任务验证：A 自动学到经验，B 加载并应用后给出反例，C 用修订版本验证。Mac 现场确认通知可见和点击，另测断线、未知投递与重启去重。当前宿主支持 Codex 0.167；未知版本保留覆盖缺口并降级，不猜测格式。
