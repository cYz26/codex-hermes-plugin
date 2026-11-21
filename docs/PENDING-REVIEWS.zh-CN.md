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

macOS 用蓝色文件夹显示项目学习，用带小地球的紫色 Codex + Hermes 自进化图标显示跨项目技能共享。普通学习回执、自动检查中的候选和已共享回执均为静音。待审批通知单独分组，显示 ⚠️、提醒附件和对应操作。在 macOS 为该发送方开启通知声音时，待审批会使用系统提示音。

同一轮复盘的待审提案合成一条项目审批提醒。若同时有生效变更、失败或重试结果，学习回执另行送达。提醒按复盘结果去重，升级前已生成的回执保留原记录。

点击记忆待审横幅或「查看项目审批」直接打开对应项目的记忆差异页。页面支持逐条批准或丢弃，也可以核对和清理重复或过期提案。原生技能写入和用户档案提案使用「查看待审说明」，明细给出 Hermes 的 `/skills pending` 或 `/memory pending` 入口。审批页只监听本机回环地址，并绑定当前浏览器会话、项目与提案版本。页面显示当前待审和最近处理结果。

项目记忆和共享文件都使用左右 Diff。当前内容在左，待审内容在右。删除和新增分别用「−」「+」及红绿底色标记，同行中的修改会进一步高亮。每处改动保留前后各三行，可展开其余未变内容。窄窗口按变更块先旧后新排列，较长内容可以展开完整原文对照。

首次打开、切回审批页或保持页面可见时，待审列表会自动更新，并显示最近更新时间。同一版本的文件和上下文保留展开状态。操作按钮显示处理进度和结果，批准共享后可在当前页看到后台发布状态。

### 通用技能待审

个人学习库开启跨项目共享检查后，可以为通过前置检查、仅缺数据分类的单项目候选选择以下审批方式。配置了专用数据策略的部署使用该策略的评估入口。

| 模式 | 自动处理范围 |
| --- | --- |
| `manual` | 由你确认候选能否跨项目共享。全新安装默认使用此模式。 |
| `smart` | 已有人工批准基线的技能，由独立审查判断同范围的文档澄清能否共享。首次共享由你确认。 |
| `autonomous` | 首次共享和后续修订都可交给独立审查。适用范围、限制和验证方法清楚的通用文档可以自动共享，重复或仅适用于本项目的内容可自动留在项目内。 |

自主模式核对具体版本和完整文件，对权限变化、适用范围扩大、长期偏好或证据不足的候选请求人工决定。脚本等超出文档审查范围的资源仍执行各自的发布检查。记忆和命令审批使用各自的设置。

```sh
scripts/bridge publication-policy
scripts/bridge publication-policy autonomous
scripts/bridge publication-review --cwd /absolute/project list
```

策略由本机学习服务持久保存。切换为 `smart` 或 `manual` 同样使用 `publication-policy`。项目列表中的 `rollbacks` 列出可撤回的自动共享版本。撤回更新会恢复上一个可验证的共享快照，撤回首次共享会从跨项目目录移除该版本，来源项目仍保有技能。

```sh
scripts/bridge publication-review --cwd /absolute/project rollback --name <skill-name> --revision <revision-from-rollbacks>
```

点击待审横幅或「查看共享审批」直接打开来源项目的共享审批区，可核对技能名、版本和完整文件清单。首次发布时清单列出全部新增文件。展开文件可查看增删修改，旧版文件不可用时会提示并显示仍可读取的待审正文。批准显示「等待发布检查」，后台重新校验并完成发布后发送静音回执。选择「保留在本项目」会关闭该版本的待审，其他项目继续看到仍通过检查的上一个共享版本。候选的内容、清单或来源变化后，旧决定失效。

### 本机学习服务

按[安装与更新](./INSTALL.zh-CN.md#更新现有学习服务)更新本机学习服务，再运行 `scripts/bridge install-notifier` 安装「Hermes 学习」与「Hermes 全局共享」两个通知 App 及审批页。首次使用全局发送方时，在 macOS 允许通知。审批页使用 Hermes 学习 LaunchAgent 的配置，读取回执所属项目的待审内容。

### SSH 接收器

已配置 SSH 接收器的 Mac 可运行：

```sh
python3 desktop/setup_reviews.py --dry-run
python3 desktop/setup_reviews.py
python3 desktop/verify_reviews.py --cwd /absolute/project --show
```

安装器复用现有接收器配置，备份相关审批资源并重建受管通知 App。权限与历史发送方保护继续使用原有安装器；失败时恢复审批资源并留下 `review-setup-report.json`。接收器无需为这次资源更新重启。

如需启用通用技能待审的独立通知，使用更新后的[接收器安装入口](./SSH-NOTIFICATIONS.zh-CN.md#在运行-codex-desktop-的-mac-上安装)同步更新接收程序，沿用原有 SSH 配置。

连接使用已配置的 SSH 别名。客户端从服务状态读取当前运行版本的审批入口。

## 验证边界

```sh
scripts/bridge approvals self-test --settings /absolute/learning/bridge.json
python3 -B scripts/check_release.py
```

隔离自检在临时学习 home 中使用真实原生工具，验证批准、丢弃、重复清理、条目变化和提案版本变化；不会启动模型或修改现有学习数据。Mac 验收程序另检查资源字节、App 标识、协议、源码摘要和代码签名；`--show` 只打开明确标记的本地显示样例，不发布通知、不批准真实提案。

实际 Mac 验收仍需观察：通知窗口能打开审批页、差异与按钮可见、取消确认不提交决定。程序启动、签名通过和隔离测试均不能代替这次界面观察。验收报告保存为接收器目录中的 `review-verification-report.json`。

候选技能内容检查支持 `When to Use`、`Use when`、何时使用和适用场景等表述。缺少限制或验证说明仍不通过；可执行资源仍需独立沙箱验证。验证器摘要参与发布缓存版本，升级后会重查旧候选，避免沿用旧的失败结果。
