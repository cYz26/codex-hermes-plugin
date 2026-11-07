# 真实 Codex App 验收任务

此目录是业务测试项目。测试里的 receipt 均为样例，不能作为真实采集或学习的证据。只有实际 App hooks、后台收据和后续聊天输出才能证明集成闭环。任务提示不要求手动总结，不要求直接调用学习复盘，也不授权修改原生记忆或技能。

## 聊天 A：执行任务

在本工程的 `pilot-project` 中实现 `receipt_report.py`，提供 `describe_receipt(receipt: dict) -> dict`，通过现有 `test_receipt_report.py`。报告要区分 synthetic probe 与真实 App 采集，未知的 usage 和实际费用保持 `None`；技能的加载、采用和验证分别报告；失败时保留已成功的写入数。先运行现有测试确认失败，再实现、重跑并报告真实结果。只修改这个目录的业务实现，不安装依赖、不提交代码、不修改运行配置，也不要手动写学习记忆或技能。

## 聊天 B：后续任务

为 `pilot-project/receipt_report.py` 增加 `render_markdown(receipt: dict) -> str`，通过 `describe_receipt` 生成简洁中文报告。分别列出采集证据、复盘状态、已生效变更、技能阶段、API 调用次数和实际费用；未知值显示“未知”，不能把估算费用当实际费用。补充覆盖 synthetic 样例、完整 App 身份样例、部分失败及未知费用的测试并实际运行。只修改这个目录，不安装依赖、不提交代码、不修改运行配置。

这两个聊天顺序执行：A 的后台自动复盘有明确终态之后再启动 B。是否加载了经验、是否实际采用、是否得到验证，分别凭实际证据记录。
