# 验证材料与证据边界

当前基线为 Hermes v0.22.7＋Learning API v1，外围插件 0.2.2。[ACCEPTANCE.json](../ACCEPTANCE.json) 记录各版本的验收边界，[INDEX.json](./INDEX.json) 记录公开范围和原始字节 SHA-256。下表为初版基线，新增实测见 [0.2.2 验收](../docs/RELEASE-0.2.2.zh-CN.md)。

| 类型 | 记录 |
| --- | --- |
| 官方原版相关回归，328 通过 | [日志](./v0.22.7/native-baseline.log)、[来源验证](./v0.22.7/native-baseline.json) |
| 补丁版相关回归，343 通过 | [日志](./v0.22.7/native-tests.log)、[测试文件列表](./v0.22.7/test-files.json) |
| 认证专项，65 通过（已包含在综合范围） | [日志](./v0.22.7/auth-tests.log) |
| 外围，9 通过 | [日志](./v0.22.7/bridge-tests.log) |
| 原生 14 项检查 | [日志](./v0.22.7/native-check.log)、[质量摘要](./v0.22.7/quality.json) |
| 真实模型 fixture | [原生结果](./v0.22.7/real-learning.json)、[调用摘要](./v0.22.7/real-learning.log) |
| 原生认证复用与隔离 | [认证状态](./v0.22.7/auth-reuse.json)、[元数据指纹比较](./v0.22.7/data-isolation.json) |
| 已安装 stdio MCP 与服务 | [MCP 摘要](./v0.22.7/installed-mcp-summary.json)、[服务摘要](./v0.22.7/service-summary.json) |
| 红→绿回归 | [profile 启动修复前](./v0.22.7/profile-red.log)、[批量结果修复前](./v0.22.7/batch-red.log)、[修复后](./v0.22.7/batch-green.log)、[usage 重启修复前](./v0.22.7/usage-restart-red.log) |

红色日志是故意复现问题的历史证据，最终综合回归为 0 失败。原生 loopback 测试使用脚本化 HTTP provider；真实 provider fixture 使用实际登录和模型，但不是 Codex App hooks 采集。配置写入、实际模型调用、App hooks、技能交付与实际采用分别作为证据，不能互相替代。

`archive/` 保存 v0.22.6 相关记录和更新前的 v0.22.7 补丁应用检查；它们不代表当前补丁的最终内容。当前补丁的权威摘要是 [manifest](../patches/manifest.json)。

完整 MCP 回执、本机服务状态、稀疏克隆诊断及详细验收状态保存在本地 `evidence/private/`，不入库。可提交的 MCP／服务摘要保留结果、产物版本、内容 hash 和原记录摘要，移除 live delivery token 与无关运行载荷。回退快照和认证文件也不在本仓库中。
