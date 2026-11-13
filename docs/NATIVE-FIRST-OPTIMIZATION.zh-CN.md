# Hermes 原生优先的优化与验收

本方案承接用户于 2026-11-14 批准的优化路线。Hermes 持有复盘、Memory、Skills、Curator、
写入保护、归档与恢复；Codex 负责实际宿主证据、项目边界、结果审查和应用验证。
当前 Learning API 是插件对 Hermes 的适配补丁，不是无补丁的上游 API。

## 执行顺序

1. 通过现有显式学习队列整理 `codebase-inspection`，核对所有资源、来源和具体版本，再做
   Hermes 项目与另一个真实项目的召回验收。随后按同样流程处理 `github` 和
   `agent-integration-acceptance`。实际宿主交付必须在对应工作区发生；只读路由探针单列。
2. 由 Hermes 原生 `skill_manage` 精简 `agent-integration-acceptance` 和 `hermes-agent`。
   主文至少缩短 50%，专题放入按需参考，记录规则映射，保留已有管理归属。已有技能名可直接
   复用；只有新建目标要求 `learned-<topic>`。用户专属约束留在适用的私有范围。
   原技能含可执行资源或源项目专属规则时，使用 Hermes 原生工具创建明确命名的纯文档共享
   变体（`learned-github-workflow`、`learned-agent-integration-acceptance`），保留原版及其管理归属。
   新变体沿用 user_taught 管理规则；不自动 adopt，也不继承原脚本或旧版的验证结论。
3. 同步完成 3 个有活动日的可靠性观察，等待自然待审提案完成一次真实通知验收；不制造提案。
4. 收集至少 3 个独立自然任务，覆盖 2 个项目、2 类技能或任务。精确版本、实际采用、
   验证和收益分别留证；本轮整理和合成探针单列，不补造业务样本。
5. 先完成 3 份真实日报（至少 2 份连续），再接每日 09:30 调度。补报不算按时连续交付。
   Curator 模型合并、质量执行、语义准入和留存仍按需逐项试点，当前生产开关不变。

## 精确版本的共享审查

`publication-preview` 原本只读；在未配置资料分类器的个人库中，通用候选会被
`data_classification_unassessed` 保留。显式整理和 `learning_scope: global_candidate`
都不构成发布检查通过。

在用户明确授权整理并共享、审查者已核对完整候选后，可提交以下本地审查 JSON：

```json
{
  "name": "example-skill",
  "version": "exact-native-artifact-version",
  "files": {"SKILL.md": "sha256-of-file"},
  "sources": ["/absolute/source-project"],
  "decision": "approve_generic",
  "note": "完整逐文件审查结论、适用范围与限制",
  "evidence": "/absolute/local-audit-report.md"
}
```

`files` 必须是完整 manifest，`sources` 是排序后的实际来源。版本、manifest 或来源变化
即失效。该入口是插件的操作员分类记录；它不修改技能、不调用模型，也不替代已有资料策略。
有非空 `data_policy` 的部署继续使用其既有评估流程。

```sh
scripts/bridge publication-preview --cwd /absolute/source-project --review /absolute/review.json
scripts/bridge publication-preview --cwd /absolute/source-project --review /absolute/review.json --apply
```

默认仅预览；应用只登记给唯一 worker。最终共享仍通过原有私有内容、来源、资源哈希、
可执行资源、生命周期和失败验证检查。新版本不继承审查结论，候选拒绝时旧发布保持可用。
没有新增 MCP 工具或知识库表；审查历史保存在桥接 meta 中，原生知识及账本不变。

## 原生未完成复盘的核对结案

实际整理中，原生 16 轮上限返回 `completed=false`，Learning API 将终态保留为 `unknown`。
该作用域随后拒绝新的显式学习，即使已确认的写入全部核对完成。适配补丁现保留原生
`turn_exit_reason`，并提供严格限定的 `close_review_without_retry`：只接受原作用域的显式
技能复盘、完整结果摘要及全部当前产物版本；存在 uncertain、待审、拒绝或记忆操作时不接受。
原 attempt 内容和 unknown 状态保留，intent 记录结案处置，旧请求仍不可重放。

操作员逐项核对后，用已有 `review-retry` 命令提交检查文件（默认预览）：

```sh
scripts/bridge review-retry --attempt EXACT_ATTEMPT --close-without-retry \
  --cwd /absolute/original-project --inspection /absolute/inspection.json --apply
```

JSON 包含 `request_id`、`attempt_id`、`outcome_hash`（原生结果 payload 的 fingerprint）、
`artifact_versions`、`note` 和 `evidence`。由串行 worker 调用原生结案方法并记录回读；
文件变化、证据身份不符、同一结案被改写都会拒绝。该能力是 Learning API 补丁适配，
不是上游无补丁功能。没有调高原生轮次或复盘总时限，也没有放宽未知写入保护。

只有因上述作用域保护而在原生准入前失败、没有 intent 或执行结果的 `learn` 请求，才可在
结案后再次调用既有 `start_learning`，复用原请求 ID 和已封存材料。已有执行或未知写入的
请求不能通过这个入口重跑。历史 unknown 不会因升级自动结案或被改写为成功。

## 只读日审与完成记录

使用 [现有日审方案](./SELF-EVOLUTION-REVIEW-PLAN.zh-CN.md) 的窗口和指标口径：

```sh
python3 -B scripts/self_evolution_audit.py \
  --home /absolute/learning-home --out /absolute/audits/daily/evidence \
  --date 2026-11-13 --timezone Asia/Shanghai \
  --audit-chat ACTUAL_AUDIT_CHAT --acceptance-chat ACTUAL_ACCEPTANCE_CHAT
```

导出读取 SQLite 的只读事务及既有回执，不实例化 Hermes。首次建立保留记录基线；后续
增量读取新回执、新尝试，并追踪未终结尝试和迟到回执。报告包含完整元数据台账、原生执行与
写入状态、恢复处置、输入裁剪、技能版本/文件哈希差异、应用记录及既有按日成本口径。
未标注聊天保持 unclassified，不能直接算自然任务。各来源有独立采样时间，不宣称跨库原子快照。
执行已完成但包含待审或拒绝写入的尝试仍列入历史操作核对，不能直接认定当前仍有待审提案。
同一受审日多次采集时保留全部观察到的变更文件；该集合不是任意两版之间的净差异。

原生尝试没有时间戳时，用关联回执注明时间依据；无法关联的时间保持未知。
无来源、超时和缺失数据仍输出 partial 报告，成功采集游标不前移。默认采集时限 600 秒。
最新发布索引 `latest.json` 和 `collection.json` 中最后完成审查日期分别维护；重复导出
按受审日期更新，补报不会把最新入口移回旧日期。JSON/Markdown 是证据汇总，语义审查默认 pending。

人工审查后，用该 JSON 的 SHA256 绑定结论文件：

```sh
python3 -B scripts/self_evolution_audit.py \
  --home /absolute/learning-home --out /absolute/audits/daily/evidence --date 2026-11-13 \
  --review-note /absolute/review-conclusion.md --snapshot-sha256 INSPECTED_JSON_SHA256
```

此操作保留已审快照并推进独立完成记录，不代表没有覆盖缺口。报告与完整证据保存在本地
受控目录；仅实现、测试和不含私有正文的方案进入 Git。到达连续日报和自然任务门槛前，
不创建每日自动任务，也不把两周观察宣称已经完成。
