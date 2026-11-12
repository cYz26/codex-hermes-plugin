# 待审记忆处理与验证

0.8.0 为原生 Hermes 的待审记忆增加项目范围内的查询、差异展示、批准／丢弃、清理预览和处理记录。后台新增事实仍遵循原有写入策略；修订或删除已有记忆继续经过原生待审机制。通知已读不会批准任何修改。

## 开发机入口

每次查询与决定都明确指定同一份学习配置和目标项目 cwd，不切到主 Hermes 的 home。

```sh
scripts/bridge approvals list --settings /absolute/learning/bridge.json --cwd /absolute/project
scripts/bridge approvals show --settings /absolute/learning/bridge.json --cwd /absolute/project --id <id>
scripts/bridge approvals approve --settings /absolute/learning/bridge.json --cwd /absolute/project --id <id> --revision <revision-from-show>
scripts/bridge approvals reject --settings /absolute/learning/bridge.json --cwd /absolute/project --id <id> --revision <revision-from-show>
scripts/bridge approvals history --settings /absolute/learning/bridge.json --cwd /absolute/project
```

`show` 返回修改前、修改后、状态和 `revision`。批准或丢弃必须使用看过的那份版本；内容变化后会拒绝提交并要求重新核对。批准调用原生 `apply_memory_pending`，使用原生记忆锁、容量与条目校验。原条目变化后不会按旧搜索片段改写新条目。成功结果保存在处理记录中，批准后的条目继续保留项目与企业范围归属。

MCP 的 `pending_reviews(cwd, id?)` 只提供项目范围内的查询，不提供自动批准工具。Agent 只能在用户明确决定某条提案后调用对应 CLI；修复插件或运行自检不代表用户批准了现有提案。

## 重复与过期提案

相同修改保留一份主提案，其他显示为重复；后台复盘不会再次提交完全相同的待审修订。原条目变化的提案显示为过期，新内容已存在的旧提案可丢弃。针对同一原条目但内容不同的提案需要分别核对，不能视为重复自动合并。

```sh
scripts/bridge approvals prune --settings /absolute/learning/bridge.json --cwd /absolute/project
scripts/bridge approvals prune --settings /absolute/learning/bridge.json --cwd /absolute/project --apply --revision <review_revision-from-preview>
```

默认只返回清理预览。执行时绑定同一份队列版本，只通过原生丢弃接口移除已核对的重复、过期或已包含新内容的提案，不修改记忆。每次最多处理 20 项，保留主提案；队列变化则需要刷新预览。

## Mac 桌面入口

通知窗口的「查看当前待审」打开该项目的浏览器审批页。页面展示完整差异，支持逐条批准或丢弃，也可核对并清理重复与过期提案。审批页只监听本机回环地址，并绑定当前浏览器会话、项目与提案版本。历史回执保留生成时的结果，当前待审状态与最近处理结果以审批页为准。

### 本机学习服务

运行 `scripts/bridge install-notifier` 更新通知 App 和随包安装的审批页。打开本机回执时，审批页使用现有 Hermes 学习 LaunchAgent 的配置，读取该回执所属项目的待审记忆。

### SSH 接收器

已配置 SSH 接收器的 Mac 可运行：

```sh
python3 desktop/setup_reviews.py --dry-run
python3 desktop/setup_reviews.py
python3 desktop/verify_reviews.py --cwd /absolute/project --show
```

安装器复用现有接收器配置，备份相关审批资源并重建受管通知 App。权限与历史发送方保护继续使用原有安装器；失败时恢复审批资源并留下 `review-setup-report.json`。接收器无需为这次资源更新重启。

连接使用已配置的 SSH 别名。客户端从服务状态读取当前运行版本的审批入口。

## 验证边界

```sh
scripts/bridge approvals self-test --settings /absolute/learning/bridge.json
python3 -B scripts/check_release.py
```

隔离自检在临时学习 home 中使用真实原生工具，验证批准、丢弃、重复清理、条目变化和提案版本变化；不会启动模型或修改现有学习数据。Mac 验收程序另检查资源字节、App 标识、协议、源码摘要和代码签名；`--show` 只打开明确标记的本地显示样例，不发布通知、不批准真实提案。

实际 Mac 验收仍需观察：通知窗口能打开审批页、差异与按钮可见、取消确认不提交决定。程序启动、签名通过和隔离测试均不能代替这次界面观察。验收报告保存为接收器目录中的 `review-verification-report.json`。

候选技能内容检查支持 `When to Use`、`Use when`、何时使用和适用场景等表述。缺少限制或验证说明仍不通过；可执行资源仍需独立沙箱验证。验证器摘要参与发布缓存版本，升级后会重查旧候选，避免沿用旧的失败结果。
