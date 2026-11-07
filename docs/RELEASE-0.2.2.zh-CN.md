# Hermes Learning 0.2.2 验收

Codex App 执行业务任务，Hermes 负责自动复盘、记忆、技能和维护。学习服务运行官方 Hermes v0.22.7 加 Learning API v1 补丁，复用现有 Hermes 的 OpenAI 登录。独立学习目录继续存放经验和会话证据。

## 真实运行验证

| 项目 | 验收结果 |
| --- | --- |
| Curator 到期维护 | 在隔离学习库中回拨测试时间，正常 worker 轮询先跳过未到期和活跃情形，再执行一次原生维护。1 项技能标为陈旧，1 项归档且原文件字节一致，近期和锁定的技能保持活跃。下一轮轮询未重复维护。默认关闭模型整理，这次维护用量为零。[Curator 回执](../evidence/v0.22.7/final-curator-due.json) |
| 点击系统通知 | 用户点击真实 macOS 测试通知，打开「Hermes 学习明细」。窗口中的唯一标识与发送时一致，自动化读回确认了窗口和标识。首次通知已送达，系统以 `display shared` 为由静默了横幅。重发后点击通过。[点击回执](../evidence/v0.22.7/final-notification-click.json) |
| 256-token 上限 | 限制完整 `additionalContext` 的 UTF-8 字节数。字节级 BPE 的普通 token 至少占一个字节，故单次 payload 上限为 256 tokens。三组已安装入口实测为 256、255、255 字节。宿主的消息封装和按需读取的 MCP 返回另计。[上限回执](../evidence/v0.22.7/final-hook-budget.json) |
| 全局范围 | 0.2.1 已完成两项目各自学习、各自召回和通用技能共享。0.2.2 用原训练证据自动复验。A 通过项目范围 MCP 取回，B 直接从 hook 摘要取回。两边实际注入各 256 字节，均核对了项目标签、原生投影和另一项目标签的缺席。[App 复验](../evidence/v0.22.7/final-app-recall.json) · [训练闭环](./GLOBAL-ROLLOUT.zh-CN.md) |

项目 A 首次 MCP 查询因后台连接被拒绝而失败。macOS CPython 3.14 的 worker 采样显示复盘线程停在可选 C YAML 解析扩展，IPC 接收线程等待 GIL。学习入口在导入 Hermes 前让 ruamel 使用其 Python 解析器。高负载下本机的学习 LaunchAgent 使用 Standard 调度类别。重启后，检查点中的 649 条待投递事件全部补投，原复盘保留中断状态，项目 A 自动复验通过。[后台恢复](../evidence/v0.22.7/final-worker-recovery.json)

## 同任务成本

两组实验分别校验 10 条和 1 条本地回执。每组的两侧使用同一目录、同一提示、逐条相同命令和相同文件哈希，指定 `gpt-6.1-sol`、Max。原生侧直接运行未经修改的官方 v0.22.7 AIAgent，使用 terminal、skills、memory。桥接侧使用当前宿主配置的 Codex CLI 和已信任的真实插件 hooks，独立 worker 自动运行 Hermes 复盘。CLI 负责采集可核对的任务用量，App 界面、召回和通知另有实测。

| 工作负载 | 流程 | 未缓存输入 | 缓存读取 | 输出 | 复盘调用 | 用时（秒） |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 10 条，原生阈值 | 原生 Hermes | 20,263 | 72,448 | 1,731 | 5 | 109.41 |
| 10 条，原生阈值 | Codex＋Hermes | 51,341 | 372,224 | 2,565 | 5 | 173.67 |
| 1 条，原生阈值以下 | 原生 Hermes | 4,831 | 4,352 | 66 | 0 | 19.63 |
| 1 条，空闲补充 | Codex＋Hermes | 46,069 | 55,168 | 1,882 | 3 | 232.98 |

10 条场景中，桥接合计多用了 31,078 个未缓存输入 token，其中复盘多用 2,531 个。两侧各有 5 次复盘调用，主要差异出现在执行任务的宿主输入。1 条场景中的空闲补充本身用了 14,281 个未缓存输入、24,192 个缓存读取和 1,807 个输出 token，用时包含 120 秒空闲门槛。两组结果分别保留主任务、复盘和原生标题生成的分项。 [原生阈值](../evidence/v0.22.7/final-cost-threshold.json) · [空闲补充](../evidence/v0.22.7/final-cost-idle.json)

这是当前两套宿主配置各一次的配对结果。工具集、宿主提示、缓存状态和模型输出会影响其他任务的差额。输出包含已报告的推理 token，合计不重复累加。OAuth 订阅没有返回逐任务实付金额，因此记录为未知。

## 复验

```sh
python3 -B scripts/check_release.py
/absolute/hermes-runtime/bin/python -B probes/curator_due.py --out /tmp/curator-due.json
python3 -B probes/paired_cost.py --run-dir /tmp/hcl-compare-10 --cases 10 --out /tmp/hcl-compare-10.json
python3 -B probes/paired_cost.py --run-dir /tmp/hcl-compare-1 --cases 1 --out /tmp/hcl-compare-1.json
```

成本探针需要已有 Hermes 登录、官方干净 checkout 和已信任的本地 hooks。两条命令使用新的短路径，隔离任务工作区和学习数据，复用 Hermes 原生认证。App 自动召回验收见 [运行流程](./GLOBAL-ROLLOUT.zh-CN.md#按需自动验收)。通知验证应使用带唯一标识的测试回执并执行真实点击。macOS 可根据当前显示器共享设置静默通知。[Apple 通知设置](https://support.apple.com/guide/mac-help/mh40583/mac)
