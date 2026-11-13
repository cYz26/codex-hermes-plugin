# 主动复盘与资料学习（0.7.0）

用户可以请求 Hermes 现在学习。`hermes-refine` 复盘已执行任务，`hermes-learn`
从指定文档、代码、URL 和笔记提炼技能。Codex 使用现有工具采集资料；既有串行
Hermes worker 完成复盘、Memory／Skills 写入和受检查的版本发布。

本变更实施用户在原生 `/refine`、`/learn` 对比研究后提出的“补充和完善这些能力”。
它扩展本插件的 Learning API 补丁；没有改变 Hermes 上游版本、模型或后台自动学习配置。

## 使用

```text
$hermes-refine 重点学习刚才定位根因和验证修复的方法
$hermes-learn 学习我指定的 SDK 目录，重点是认证和分页，跳过废弃接口
```

MCP 工具：

| 工具 | 行为 |
| --- | --- |
| `request_learning` | 绑定 cwd、真实 chat_id、request_id 和 focus；refine 固定证据并排队，learn 开始收集资料 |
| `add_learning_material` | 添加有来源、标题和稳定 chunk_id 的 UTF-8 资料块 |
| `start_learning` | 封存资料并启动分批学习及索引核对 |
| `learning_request_status` | 查询单次请求、批次、实际写入、暂存和发布结果 |
| `cancel_learning` | 停止排队任务与后续批次；运行中的批次可能结束，已写入内容保留 |

同一请求重试使用原 request_id；用户再次要求学习时使用新的 ID，可以重复学习同一份证据。
`scope=memory|skills|both` 控制写入范围。默认 `memory_writes=add`；用户明确要求修正
旧项目记忆时可以选择 `revise`，仍遵守原生写入策略和项目范围。

Codex 的 `CODEX_THREAD_ID` 提供当前宿主聊天 ID。不能只凭 cwd 选择该目录最近的聊天。
默认仅使用提交时已观察到终态的回合。指定尚未结束的 `through_turn` 时返回
`waiting_for_turn`，由后台在真实终态到达后处理；当前回合不能同步等待自己结束。

CLI 与 MCP 共用同一服务，安装包中可用：

```sh
scripts/bridge learning request --cwd /ABS/PROJECT --request-id refine-001 --focus '根因与验证'
scripts/bridge learning status --cwd /ABS/PROJECT --request-id refine-001

scripts/bridge learning request --cwd /ABS/PROJECT --request-id learn-001 --mode learn --skill-name learned-api-auth --focus '认证流程'
scripts/bridge learning material --cwd /ABS/PROJECT --request-id learn-001 --chunk-id auth --source /ABS/selected-doc.md --file /ABS/auth-chunk.txt
scripts/bridge learning start --cwd /ABS/PROJECT --request-id learn-001
scripts/bridge learning status --cwd /ABS/PROJECT --request-id learn-001
```

CLI 的 `--chat` 默认读取 `CODEX_THREAD_ID`，普通终端需要显式传入真实聊天 ID。
每个动作支持 `--settings`，遵循已有配置定位。上面的 ID 是示例，新任务应使用新 ID。

## 资料边界与结果

显式资料学习首次创建的目标技能沿用原生 `/learn` 的管理归属。批次回执的 `curator_management=user_taught` 表示原生记录已确认，用户可通过原生 Curator 的 adopt 入口交给自动维护。

每个资料块最多 24 KiB UTF-8，一个请求最多 64 块。按章节／主题读取并提交，避免整份资料
超过单次 64 KiB 复盘输入边界。后台每批生成一个 `references/` 文件，最后核对目标技能
的 SKILL.md 索引。超出 64 块时，后续请求继续使用同一目标技能。

排队、复盘结束、内容写入、版本发布和业务验证是不同状态。结果返回每批原生
applied／staged／uncertain 项、产物版本和共享结果；`reference_check` 只证明引用文件
及索引存在。无写入的复盘不构成新技能，脚本仍需现有独立隔离验证。

请求及材料遵循现有资料准入、企业范围、凭据过滤和显式禁学约束。复盘快照、材料批次
均绑定实际项目与聊天。自动学习关闭不阻止显式请求；服务暂停、更新排空、写入规则
和预算仍生效。运行时缺少扩展 API 时，状态给出 `runtime_upgrade_required`。

## 实现与恢复

外围 SQLite 保存请求、材料及批次进度；原生 Learning API 添加幂等 `request_review`、
按请求读取和排队取消能力，快照独立于 automatic revision 水位。手动请求不伪造
observation、不增加自动阈值计数、不把自动证据标成已复盘。

worker 使用既有 review／provenance／publication 流程。请求 ID 与 attempt ID 持久化，
原生完成但外围尚未发布时可恢复缓存结果；unknown 不自动重放。取消停止将来的工作，
不承诺回滚已有写入。新表为附加表，旧版代码忽略它们；回退前先排空执行中的请求，
保留原生 journal、外围事件箱及新增知识，不恢复旧知识快照覆盖新写入。

交付通过原生与外围离线合同、独立 provider 夹具及新进程 MCP 分别验证。运行中的
Codex 聊天不保证热加载新工具；可使用安装包 CLI，或在新聊天中使用新的 skills/MCP。
实际执行过的验证范围以本次发布记录为准。

本次[验证记录](../evidence/active-learning/0.7.0.json)：343 项原生测试和 222 项外围／集成
测试通过。现有企业模型完成两批资料学习、索引核对和项目记忆写入；首次复盘因
HTTP 429 未执行成功，保留原记录后的一次有界补测通过。这些使用独立的合成测试资料，
不等于真实 Codex App 会话验收或业务效果提升。
