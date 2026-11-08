# v0.23.0 适配核对

目标为官方 [Hermes v0.23.0](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.0)，提交 `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`。官方 annotated tag 的提交和源码根树 `675b4cea2d2aef80460f07235b0e27e6c6922743` 均已核对。比较基线为当前 Learning API 的官方 v0.22.7。

初次 GitHub compare 只返回 300 个文件，因此该结果没有直接放行。随后核对完整源码，检查 review、触发、记忆/技能工具、Curator、插件能力和状态实现；结论是本次不需要重设计自进化流程。

| 变化 | 本插件影响 |
| --- | --- |
| `background_review.py` 为后台复盘绑定不交互的 secret capture | 避免技能缺少环境变量时向用户弹出输入面板；保留上游改进 |
| `turn_context.py` 增加 bot chain 的回合入口 | 没有改变本 Learning API 的外部观察计数与复盘合同 |
| 原生插件批量/自动更新增加锁及能力扩展审查 | 属于原生插件管理；本项目仍通过注册 CLI 和 Learning API 运行独立 worker |
| skills hub 来源/信任缓存及测试状态保护改进 | 没有改变本项目所依赖的 memory/skill 写入、暂存和锁合同 |
| Curator、Learning API 依赖的计数、原生写入接口及状态结构 | 对比未发现需要本项目重设计的差异；以回归和暂停启动检查进一步把关 |

原 Learning API v1 补丁可直接重放，无需改写补丁。14 个补丁文件中，只有合并上游改进后的 `agent/background_review.py` 文件哈希不同；[适配清单](../patches/v0.23.0/manifest.json)单独记录新提交和结果哈希。

最终候选通过 19 个原生测试文件的 **333 项测试**、外围 **66 项测试**、注册插件的真实 worker 启停、暂停启动、项目作用域 context、MCP 和技能正文交付检查。切换前候选源码、适配清单、插件代码和已安装插件指纹一致。

候选测试使用脚本化 provider，属于合同及隔离运行验证。部署后另行观察到新版自然完成一次生产复盘：6 次原生模型 API 调用、2 项技能写入生效、无暂存或 uncertain；未手动触发额外学习。这证明一次新版真实复盘，不等于新一轮 Codex App A→B 验收或质量提升。原生登录仍从根账号回退读取。

安装 0.3.0 后宿主把 7 个 hooks 标为 modified，需要在 Codex 插件界面重新信任。历史 0.2.2 / v0.22.7 证据保持其原版本。更新验收见[独立记录](../evidence/updates/v0.3.0.json)；运行中的切换事务和当前状态由 `scripts/bridge update status` 读回。
