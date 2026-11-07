# 真实 Codex App 验收

更新日期：2026-11-05。官方 Hermes **v0.22.7＋Learning API v1**，外围插件 **0.1.6**；学习模型 **gpt-6.1-sol / openai-codex / codex_responses / Max**。本轮没有追加 Hermes 核心补丁。状态以 [ACCEPTANCE.json](../ACCEPTANCE.json) 和 [恢复指针](../VALIDATION-STATUS.json) 为准。

用户授权的单项目 A→B 闭环已通过：A 的真实任务自动进入原生学习，B 后续读取匹配版本的技能并完成业务验证。B 首轮因升级信任暂停而漏采的事实保留；用户重新信任后，同一个 B 聊天的两个只读回合已真实采集、投影并登记原生正文交付。没有导入旧转录补齐首轮。随后 B 的新证据也按原规则自动完成了第二轮学习，用户确认聊天提示与这次系统通知均可见。完整证据为 [app-closed-loop.json](../evidence/v0.22.7/app-closed-loop.json)。

## 实际结果

| 环节 | 结果 | 边界 |
| --- | --- | --- |
| 后台 profile 隐藏 | 用户确认 Sessions、Bots 均不再显示 | 后端列表和界面观察分别有记录 |
| App MCP | 本聊天、A、B 均实际调用 | 不再只凭 stdio 握手判断 |
| A 业务执行 | 缺少模块时 RED；实现后 6 项测试通过 | [A 回执](../evidence/v0.22.7/app-chat-a.json) |
| A hooks | SessionStart 1、PreToolUse 15、PostToolUse 15、Stop 2 | 含一次只读复核；没有伪造 UserPromptSubmit |
| A 自动学习 | completed，10 次成功写入，涉及 2 个技能 | 0 暂存、0 拒绝、0 不明；未强求新增记忆 |
| B 业务执行 | 增加 Markdown 报告，15 项测试通过 | [B 首轮回执](../evidence/v0.22.7/app-chat-b-initial.json) |
| B 首次召回 | 原业务提示未点名 Hermes；实际读取 context、技能正文和参考资料 | [首轮 App 读回](../evidence/v0.22.7/app-chat-b-initial.json)；当时 hooks 未采集，旧交付仍为 issued |
| B hooks | 恢复信任后 PreToolUse 5、PostToolUse 5、Stop 2，另有 SessionStart／End 各 1 | 两个只读回合均 observed、projected；[实际回执](../evidence/v0.22.7/app-chat-b-captured.json) |
| B 版本交付 | 正文、参考资料均匹配 A 版本 bbd68ab88ad6…，原生 body_delivered 已登记 | 这次显式版本核对与首轮自然召回分别记录；不据此证明质量提升 |
| B 再次自动学习 | 原生 idle_flush、4 次 API 调用、3 次成功写入 | [B 复盘回执](../evidence/v0.22.7/app-chat-b-review.json)；未手动触发、未降低阈值 |
| 系统通知 | B 复盘完成后派发成功，用户确认实际看到了通知 | 对应这次“3 项变更生效”；不将此前 A 的派发记录当作展示证据 |
| 聊天反馈 | 消费记录正常；用户明确报告看到了聊天提示 | 聊天与系统通知分别确认，未通过截图判断具体 UI 样式 |

A：`01a1a8fd-8c82-7bd1-b3b3-ca49b1bc04f6`；B：`01a1a924-e927-7582-bcfe-87a9563fcc44`。完整任务见 [APP-TASKS.zh-CN.md](../pilot-project/APP-TASKS.zh-CN.md)，没有要求手动复盘或写学习产物。业务 receipt 均为样例，不能替代真实宿主证据。

## 自动复盘与恢复

第一个原生阈值 intent 已自动触发，但 launchd 缺少 App／交互环境已有的代理设置，provider 在建立连接前连续超时。通过公开取消接口协作停止并等待 owner 释放，原生结果为 interrupted、0 写入、0 不明；保留记录，没有盲目重跑同一 intent。

外围配置现在显式接受标准代理变量，launchd 和原生 worker 使用同一环境。没有复制整个 Hermes `.env`、认证文件或 Codex 登录。未认证 HEAD 检查显示直连超时、现有代理可连接；诊断不调用模型。

随后 A 真实只读复核再次通过 6 项测试、文件 hash 一致。新回合按既定空闲规则自动复盘，没有手动调用 review。成功 attempt：`3c90bbf0-7126-4e1b-b76e-c03c8e3f9c6e`，reason 为 idle_flush，revision 2。

成功操作为 `agent-integration-acceptance` 的 2 次修订和 1 个参考文件，以及 `test-driven-development` 的 7 次修订。10 次操作不是 10 个不同技能。原生 authoring advisory 保留；参考文件后来已由同批操作写入，没有手工修补学习结果。

## 真实宿主暴露的问题及修复

1. **MCP 路径。** 0.1.3 将 `${PLUGIN_ROOT}` 留在可执行路径中，App 报 ENOENT。改为 python3、args `["scripts/bridge", "mcp"]`、cwd `"."`。包装回归先红后绿，直接执行 manifest，不展开占位符。
2. **App 初始任务。** 工具创建的子聊天没有 UserPromptSubmit。外围按真实子 chat 身份绑定父 create_thread 回执，并标记为 delegated task specification，不当作直接用户声明。
3. **后续委派回合。** send_message 未返回 turn ID；仅在真实 read_thread v1 的回合、完整 codex_delegation 与原发送回执完全匹配时关联。截断、歧义、跨项目输入拒绝关联，不猜时间窗口。
4. **空闲判定。** 完成的 A 可在本协调聊天活跃时复盘；保留 120 秒、600 秒和单 worker，Curator 仍要求整个项目空闲。
5. **内部建议。** 按原生明确标记和缺失 transcript 身份排除“建议下一条用户消息”的请求；真实用户引用且有转录身份不会被误排除。旧证据保留。
6. **启动网络。** 专用服务显式读取配置中的标准代理变量，避免 interactive shell 可连而 launchd 不可连。
7. **升级信任。** hooks 文件字节相同不保证可执行内容仍受信任。升级后官方 hooks/list 曾读回 7 项 modified；用户已通过界面重新信任，随后 7 项均为 trusted，真实事件恢复。没有代写信任或使用 bypass。

外围 20 项回归通过，与业务报告器的 15 项测试分开计数。发布检查另外核对 A 的自动复盘、B 的实际身份、原生交付和产物哈希；错误版本、未登记交付、未投影回合均会被拒绝。安装、MCP 可用、hooks 信任、采集和经验效果分别判断。

## 检查信任与服务

```sh
python3 -B scripts/check_codex_hooks.py --codex /absolute/path/to/codex
scripts/bridge status
scripts/bridge auth-status
```

[信任诊断](../scripts/check_codex_hooks.py)仅启动独立 stdio 诊断进程，调用官方 hooks/list，不接管 Desktop 内部服务、不执行 hook、不修改配置。退出 0 表示新 CLI 配置中的 7 项启用且受信任；退出 2 表示未就绪。仍需真实 App 执行证明。[升级读回](../evidence/v0.22.7/app-hooks-after-upgrade.json)保留 modified 结果。

bridge status 的 idle 只表示学习 worker 空闲，不能证明 Codex 正在采集。当前 [重新信任读回](../evidence/v0.22.7/app-hooks-retrusted.json)和后续真实事件均通过。B 的只读回合再次通过 15 项测试和 3 个直接函数断言，业务文件哈希不变；正文与参考文件各有真实 chat／turn／tool-call 身份，版本及哈希与 A 产物完全一致。首轮缺失 hooks 不回填。

## Tokens、反馈与剩余验收

两轮实际 App 复盘均完成：

| 原生 usage | A | B |
| --- | ---: | ---: |
| API 调用 | 4 | 4 |
| input_tokens | 41,833 | 23,858 |
| output_tokens | 5,938 | 3,931 |
| cache_read_tokens | 103,424 | 59,776 |
| reasoning_tokens | 3,762 | 2,517 |
| 成功写入操作 | 10 | 3 |

B 的 3 次操作继续修订同一个 agent-integration-acceptance 技能及其参考资料，形成新版本 b76376212e76…；此前 B 读取的 A 版本 bbd68ab88ad6… 发布副本仍保持原 hash。保留 usage 分项，不将 attempt 和 native ledger 重复累计。estimated_cost_usd=0.0 是估算字段；实际费用及 Codex 业务执行 usage 未知，没有声称免费或完成同任务原生成本对照。

历史 synthetic provider probe 的 6 次调用单独保留，不算作 A／B 的 8 次；当前原生复盘账本合计 14 次。通知采用程序模板，没有增加 LLM 调用。dispatched=true 只证明派发成功，不证明用户已看到。用户在恢复 hooks 后最初报告“只看到聊天提示”；A 的通知已在此前完成时派发。随后 B 按原 600 秒补充间隔自然复盘成功，实际派发“3 项变更生效”，用户再次确认“看到了系统通知”。没有为通知强制调用模型或写学习产物。

hooks 的 additionalContextLimit=256 是宿主 spill 阈值，并非严格 token 上限；外围另截取 520 个字符。技能正文按需加载。聊天和系统通知的可见性已由用户确认；严格 token 预算、正常输入框路径、完整 App 生命周期、真实到期 Curator 及质量／费用对照仍保留独立边界。

恢复材料在本机 `~/.hermes-codex-learning/backups/`，保留工作区、配置、专用服务和数据库，不包含根账号凭据。原 v0.22.7 的 328／343 原生回归及 14 项检查仍是核心基线，没有重标成 App 证据。本轮新增改动尚未提交或推送。
