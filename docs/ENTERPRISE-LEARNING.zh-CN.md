# 企业专用环境中的自动学习

企业模式采用操作人确认的部署授权：当前开发机为企业任务专用，当前企业采购模型接口获准处理普通内部任务。它使正文默认参与学习，不再要求每段内容先匹配公开／合成来源清单。这是部署级授权，不是通用 DLP 或逐条密级识别服务。

## 行为

- 任务目标、代码、文档、错误及验证结果可以自动进入本实例的复盘。
- 已知凭据字段、Bearer、JWT、私钥会在本地处理；明确 L4 或禁止学习／外部处理的材料继续拦截，不自动降级其等级。
- 项目记忆仍只返回匹配项目；可复用技能经过资源、质量和来源检查后，可以在本机企业知识库共享。
- 企业技能返回 audience=enterprise 与边界标识，不标记为 public。内部工具名、API 和文档地址不再仅因“内部”就被共享检查拒绝；绝对项目路径与凭据仍受检查。
- 模型配置、主机或运行用户不匹配时，企业正文复盘暂停，带企业边界的记忆和技能不再供外部模式读取。

## 启用和恢复

只在操作人确认上述企业模型与专用环境的条件后执行：

```sh
scripts/bridge enterprise-config --settings /ABS/pilot/bridge.json
scripts/bridge enterprise-config --settings /ABS/pilot/bridge.json --apply
scripts/bridge install --settings /ABS/pilot/bridge.json --apply --start
```

第一条只预览。第二条保存部署授权记录及原资料策略，绑定运行用户、主机与实际原生模型配置摘要；不复制账号凭据，不改变 provider、模型、推理档位和输出上限。第三条按现有排空流程重新加载服务。模型配置若变化，需要按实际批准范围重新绑定。

恢复以前的资料策略：

```sh
scripts/bridge enterprise-config --settings /ABS/pilot/bridge.json --disable
scripts/bridge enterprise-config --settings /ABS/pilot/bridge.json --disable --apply
scripts/bridge install --settings /ABS/pilot/bridge.json --apply --start
```

停用企业模式不会删除已有经验；企业内容保持带边界的存储状态，不自动变成可公开的知识。已经包含企业正文的复盘会话保持企业归属，不能由之后的公开片段改写归属；停用后新开会话可按恢复的策略继续学习。诊断中的 data_admission.mode、boundary_valid 与 unassessed_body 区分策略有效、配置变化和原先的来源白名单模式。

## 本次云端验收

[0.6.0 验收记录](../evidence/releases/cloud-0.6.0.json) 绑定最终构建及原始证据摘要：337 项原生测试、189 项桥接测试与发布检查通过；真实企业模型接口使用合成任务完成技能创建、反例修订、独立脚本验证和企业内共享，未混入其他项目记忆。最后补充的策略停用与凭据落盘保护通过回归测试。

云端已部署企业模式，原生模型配置保持不变。新构建的 7 个 hooks 当前需由用户通过 Codex 原生界面重新信任，随后才能验收 0.6.0 的实际会话自动采集。24 小时服务／策略观察已启动，尚未完成；Mac 界面与真实业务学习质量不在本次已通过范围内。
