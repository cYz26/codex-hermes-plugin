# Codex 执行、Hermes 学习：实施与验收记录

2026-11-04。首版代码、独立运行环境、插件安装和 launchd 服务已落地；真实模型与 Codex App 的 A→B 学习闭环尚未验收。按用户最新要求，学习 profile 和获批方案中的推理强度均已设为 **`agent.reasoning_effort: max`**。

最新版本复核：官方已发布 **v0.22.7 / b200f5bc60e31588ba2cb60e288029c4a54ba5fa**。现有补丁所修改的 4 个官方文件在 v0.22.6→v0.22.7 间字节未变，9 个新增文件无上游同名冲突；临时官方快照的 `git apply --check` 和实际应用均成功。此结果只说明补丁可应用，v0.22.7 的原生回归与真实模型/App 验收尚未运行；以下部署和 193 项测试仍属于 v0.22.6。本次未更改运行源码 HEAD 或切换服务。[核查记录](../../evidence/archive/pre-upgrade-v0.22.7.json)。

## 已交付范围

- 官方 Hermes **v0.22.6 / `f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab`**，全新源码位于 `/Users/cy/Dev/agents-dev/hermes-learning-core`，本地分支 `feat/learning-api-v1`。
- 核心提供版本化 `ctx.learning(version=1)`。外围通过 facade 使用观察会话、原生复盘、成果读取、技能交付反馈、维护和关闭能力。
- 外围插件 **0.1.2** 提供真实 hooks 采集、持久队列、Unix socket、独立后台服务、MCP 读取、聊天反馈和 macOS 通知。
- 专用 profile `/Users/cy/.hermes-codex-learning/pilot`；本次只采集 `/Users/cy/Dev/agents-dev/hermes-plugin1102/pilot-project` 下的主聊天。
- 官方 CLI 已注册本地 marketplace 并安装插件，缓存位于 `/Users/cy/.codex/plugins/cache/hermes-learning-local/hermes-learning/0.1.2`；代码、hooks、技能与安装缓存一致。
- LaunchAgent `com.cy.hermes-codex-learning.pilot` 已启动，当前状态 `idle`，无服务错误。安装状态不等于 App 已载入或已信任 hooks。

没有使用旧定制版的源码、配置、凭据、数据或维护脚本；现有 Hermes 应用未被替换或重启。核心补丁尚未提交 Git，也未推送远程。

## 自进化如何执行

观察终态及真实工具身份先持久化。核心按原生 memory/skill 阈值产生复盘 intent；明确完成后空闲 120 秒可补充复盘，至少间隔 600 秒，并发为 1。相同来源不会因重投再次计数，同一批证据不会被阈值和空闲机制重复审阅。

复盘调用 Hermes 原生 unattended fork，使用原生提示词、provider 路由、工具、预算、技能锁、写入保护和 Curator。外围不调用模型先写总结，也不让 Hermes 重新执行业务任务。原生要求暂存的修改仍显示为暂存，不发布为生效成果。

核心与外围分别保存观察、投影、intent、attempt、关闭和交付记录。写后崩溃或 provider 投影不明时显示 `unknown` / `uncertain`，不盲目重放。journal、原生 state.db、文件和远端 provider 不构成跨存储原子事务。原生技能 usage 是 best-effort telemetry，正文交付不等于应用成功或效果已验证。

与直接使用 Hermes 的差异仍包括：任务执行历史来自外部宿主，缺失身份时明确报告覆盖缺口；Hermes 未预热 Codex 主任务的模型缓存；空闲补充属于额外触发；Codex 的提示样式及注入时机取决于宿主 hooks。v1 仅支持 POSIX、in-process、单 profile 单 owner，未验收子 agent 和跨机器部署。

## 模型与反馈配置

```yaml
model:
  provider: openai-codex
  default: gpt-6.1-sol
  api_mode: codex_responses
agent:
  reasoning_effort: max
auxiliary:
  background_review:
    enabled: true
    provider: auto
    model: ""
```

原生复盘继承该 Hermes profile 的模型和 Max 设置；不读取 Codex App 当前模型选择。新 profile 禁用外部登录自动接管，认证由官方 Hermes OAuth 流程管理。

聊天 `systemMessage`、给模型的增量 `additionalContext` 和系统通知分别消费。通知文案为程序模板，无额外模型调用。正文通过 MCP 按需读取，hook 上下文设置 256 tokens 单次宿主上限。macOS 通知仅证实程序调用已派发，系统是否实际展示仍为未知。空闲聊天的补收和实际 UI 样式尚待 App 验收。

usage 中区分原生复盘账本、单次 attempt 和外部业务执行；重叠 usage 不相加，获取不到的数据为 `null`。尚未取得真实任务的 token、缓存、费用或额度对照，不能据本地 fixture 推断成本。

## 已完成验证

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 还原官方基线的相关回归 | 6 文件，183 通过，0 失败 | [原版日志](../../evidence/archive/v0.22.6/native-baseline.log) |
| 修改后原生与扩展回归 | 9 文件，193 通过，0 失败 | [扩展日志](../../evidence/archive/v0.22.6/native-tests.log) |
| 扩展合同补充回归 | 10 通过 | [合同日志](../../evidence/archive/v0.22.6/contract-tests.log) |
| 外围桥接 | 6 通过 | [桥接日志](../../evidence/archive/v0.22.6/bridge-tests.log) |
| 已安装插件 stdio MCP → 真 daemon | initialize/list/status 通过，3 个工具 | [MCP 记录](../../evidence/archive/v0.22.6/mcp-smoke.json) |
| 核心补丁重放 | 对固定基线应用后，13 个文件逐字节匹配 | [补丁 manifest](../../patches/manifest.json) |
| 代码检查 | 原生完整 14 项通过、health 0 blocking / 0 advisory；另有外围 lint、技能验证、diff 空白检查通过 | [质量记录](../../evidence/v0.22.7/quality.json)、[完整检查日志](../../evidence/archive/v0.22.6/native-check.log) |

native-loop 测试运行真正的 Hermes 复盘循环和 memory/skill 工具，覆盖生效写入、原生暂存、工具 usage、技能交付去重及 SessionEnd。其 HTTP provider 为本地脚本 fixture，**不是实际模型质量或真实 App 证据**。官方对照在临时源码副本中还原所有修改文件并移除新增文件，使用同一隔离依赖环境运行；日志中无 Git 仓库提示来自该临时副本，测试本身 0 失败。

新增代码的额外 F/E9 检查通过。将这一额外规则用于原有文件时发现上游已有未用 import，未为此扩大修改范围。初次完整检查受稀疏克隆缺失文件影响；补齐官方基线对象和所需 tracked inventory 后，用未修改的检查器完成全部 14 项。修正了新增代码的 UTF-8 读取及异常 traceback 日志问题后，193 项回归再次通过。

历史 `HERMES-CODEX-PROBE.json` 保持原字节和 SHA-256 `79c042e1e6d3acfd91adf9a882d568197312efda248a0384bf738ee23fd03142`；不把研究期探针当成本次实现验收。旧研究文档存放在 `docs/archive/`。

## 尚待完成的真实验收

1. 在独立 profile 完成官方 OAuth。此前两次登录均因未收到本地回调而超时；本次浏览器重试仍未收到回调，已取消并改用官方设备码流程，目前等待授权结果。没有复用旧凭据，没有真实学习模型验收调用。
2. 在 Codex App 正常 UI 中审阅并信任 Hermes Learning hooks。当前电脑控制工具明确禁止操作 Codex App，因此没有绕过该限制或替用户制造信任记录。
3. 在 pilot 项目聊天 A 执行普通任务，验证真实事件身份、后台自动学习、生效／暂存／失败通知；空闲时确认系统通知的实际展示。
4. 在聊天 B 验证成果自动召回、正确技能版本正文交付和使用证据；完成实际 App 生命周期、provider drain、Curator 和重启恢复验收。
5. 比较原生阈值与原生阈值＋空闲补充的真实质量、延迟、tokens、缓存和可获得的费用信息。

这里的等待来自缺失 OAuth 回调及宿主 hooks 信任，不是额外的源码修改审批。当前机器状态见 [ACCEPTANCE.json](../../ACCEPTANCE.json)。操作步骤见 [README](../../README.md)：`scripts/bridge login`、`scripts/bridge status`。只有目标 pilot 项目启用采集。

## 升级与恢复

Hermes 升级时暂停本 pilot 的 LaunchAgent，留存一致性 profile 快照，在新官方稳定版候选源码上重放 `patches/native-learning-api-v1.patch`，运行原生与固定插件合同回归，再切换服务。数据兼容性和回滚单独验证；不调用旧定制版更新器。

Codex 升级时验证 hook 字段、身份、信任、提示和 MCP；插件版本变化可能需要重新信任。未知 attempt 不自动重试，先核对原生写入与执行记录。配置命令拒绝覆盖已有 profile；解释器必须保持 `/Users/cy/.hermes-codex-learning/runtime/bin/python` 的 venv 路径。
