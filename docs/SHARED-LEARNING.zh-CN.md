# Codex 与原生 Hermes 共用学习服务

共享学习使用一个 Hermes 学习服务。Codex 和原生 Hermes CLI、Desktop 各自执行任务、保留会话和运行配置。原生客户端通过本机 Unix socket 提交实际的回合与工具证据，并读取该服务管理的记忆和技能。适配版本为固定的 Hermes v0.23.1，补丁由现有候选升级流程验证。

## 使用范围

- 项目记忆按当前工作区的 Git 根或目录读取。无工作区时，仅提供用户记忆和已发布通用技能。两端使用相同项目路径时，共用同一个原生 MemoryStore。
- 每个项目的 `MEMORY.md` 和用户的 `USER.md` 各有独立容量。用户级写入走原生待审队列。两端的技能读取使用相同的已发布或当前项目专属版本。
- `/memory pending`、`/memory diff <id>`、`/memory approve <id>`、`/memory reject <id>` 查询同一个队列。原生 `/skills` 的发布前写入审批和既有 Codex 通用技能发布审批仍各自承担原本的职责。
- Journey 编辑、删除和技能归档携带当前版本。Desktop 启动时通过 `--cwd` 选定 Journey 的管理工作区。聊天的学习与记忆使用实际会话工作区。切换工作区后创建新会话加载对应快照。
- 在现有官方 Desktop 中，从 Journey 编辑共享技能。旧的技能详情编辑表单只传技能名，无法校验打开表单时的版本。技能详情仍可直接读取。
- 共享历史通过原生 SessionDB 读取该项目获准保留的观察记录。技能加载及宿主测试结果按精确版本关联。状态显示加载证据，不以加载次数判断业务效果。

## 准备与启用

使用已验证的 v0.23.1 共享学习候选更新现有学习服务。安装步骤和原生版本检查见[安装与更新](./INSTALL.zh-CN.md)及[升级合同](./UPDATES.zh-CN.md)。可使用现有 Hermes profile 命令创建独立的本机执行 profile，例如 `shared-learning`。它可按原生规则使用已有 Hermes 登录。

在原学习服务和原生执行 profile 都处于任务边界时运行盘点，再让 worker 停在可切换状态并绑定。盘点包括技能、资源、记忆和待审项的文件指纹，以及同名技能差异。绑定生成原配置和两端知识的私有备份，并在下一次启动时分配作用域存储。只有已有精确项目来源的旧项目记忆和待审提案会迁移。来源不明、个人及原生 profile 的旧条目留在原位置，适用内容可通过 Memory 或技能审批逐项纳入。

```sh
scripts/bridge shared-learning inventory --settings /absolute/pilot/bridge.json --profile /absolute/hermes/profile
scripts/bridge shared-learning bind --settings /absolute/pilot/bridge.json --profile /absolute/hermes/profile
scripts/bridge shared-learning bind --settings /absolute/pilot/bridge.json --profile /absolute/hermes/profile --apply
```

启动正常学习服务，用新会话读取记忆快照。从相应工作区启动 CLI。等正在使用的 Hermes Desktop 正常退出后，用 `desktop` 命令启动已经安装的官方应用，保留原有界面数据。启动参数会指定已验证的 Python 后端、已绑定的 profile，以及 Journey 的管理工作区。从官方项目入口创建聊天，核对聊天本身的项目名称和路径。

```sh
scripts/bridge shared-learning cli --settings /absolute/pilot/bridge.json -- chat
scripts/bridge shared-learning desktop --settings /absolute/pilot/bridge.json --cwd /absolute/workspace
scripts/bridge shared-learning status --settings /absolute/pilot/bridge.json --cwd /absolute/workspace
```

现有项目审批页的「共享学习」区域显示两端已接收的回合数、处理中的请求、待核对结果和已发布或已加载的技能版本。服务暂离时继续执行任务，持久队列在连接恢复后按原顺序重试。写入回执区分已应用、待审、冲突、排队、失败和需核对。超过会话边界的旧记忆快照要通过新会话刷新。

## 撤回、恢复与验证

指定项目后可使用 Journey 删除相应记忆或归档技能。CLI 清空须指定 `--target memory` 或 `--target user`，并确认显示的范围。已删除的项目记忆内容留下无正文指纹，避免旧请求或复盘重新写入。恢复技能使用原生归档机制，跨项目仍经过当前发布检查。

通过下列命令停用原生客户端。关闭已连接的原生会话后重启即可单独使用原生 profile。共享知识、写入回执、待审和撤回记录留在 owner 中。服务端软件回退沿用现有 `update rollback`，目标必须支持当前共享存储格式。

```sh
scripts/bridge shared-learning disconnect --settings /absolute/pilot/bridge.json --apply
```

兼容性验证以[固定兼容清单](../patches/compatibility.json)和隔离探针为准。探针启用两个临时工作区、一个共享 owner、一个原生 profile 和本机可控模型服务。它运行真实 CLI，检查两端读取的知识版本、两项目隔离、Desktop 后端的 Journey 写入与旧版本冲突。`--live-settings` 使用现有模型和虚构任务验证原生运行时。Codex App hooks、官方 Desktop 界面和自然任务的验收需要分别记录。

Codex 宿主缺少 `Stop` 回执时，服务可以用绑定后已捕获的用户请求、受信本机会话文件，以及同一回合的宿主完成记录恢复终态。测试退出码要求与原工具调用对应的原生记录。

```sh
python3 -B probes/shared_learning_contract.py --source /absolute/verified-candidate --python /absolute/runtime/bin/python
```
