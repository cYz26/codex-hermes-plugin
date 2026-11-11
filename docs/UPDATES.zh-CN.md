# Hermes 主动检查与受验证升级

本插件默认每天检查 Hermes 官方稳定版。候选通过原生学习合同、外围回归、worker 启动与 stdio MCP 检查后，在当前复盘结束时切换。从 0.3.1 起可绑定 Hermes App 的官方安装，共用它发布的 Python 依赖环境。

## v0.23.1 适配

Hermes Learning 0.3.3 跟随本机已安装的 Hermes v0.23.1。[适配清单](../patches/v0.23.1/manifest.json)固定官方提交、源码树、补丁和 14 个补丁文件的哈希。现有 Learning API v1 补丁可直接应用。上游在复盘钩子传入会话标识，并新增 Curator 完成钩子，现有触发、复盘和写入合同通过回归。

临时会话库验证了 schema 32→33 迁移，以及 v0.23.0 重新打开后读取和追加已有会话。学习服务继续使用复盘边界切换及快照回退。由插件主动发起官方更新的宿主校验仍绑定 v0.23.0。此前适配见 [v0.23.0 核对记录](./UPGRADE-v0.23.0.zh-CN.md)。

## 策略与入口

`bridge.json` 的 `updates` 对象可配置为 `{"mode": "auto", "interval_seconds": 86400}`。
`auto` 检查、验证并自动切换；`check` 只检查，后续准备和切换使用 CLI；`off` 关闭周期检查。周期范围是一小时至 30 天。修改策略后监督进程自动读回；旧版本服务首次采用监督进程需要重启一次。

```sh
scripts/bridge update status
scripts/bridge update check
scripts/bridge update prepare --version v0.23.0
scripts/bridge update apply
scripts/bridge update rollback
scripts/bridge update policy --mode check
```

每个子命令都接受 `--settings /absolute/path/bridge.json`，也支持 `HERMES_LEARNING_CONFIG`。`apply`、`rollback` 提交持久请求，由监督进程在复盘边界处理；返回 `queued` 不表示已经升级。MCP 的 `update_status`、`update_check` 分别读取状态和显式检查。即使候选解释器损坏，标准库客户端仍可读取状态并采集 hooks。

## 共用官方安装

指定当前 App 的官方源码目录后，插件会从同一固定提交准备只供学习 worker 加载的源码视图，并应用 Learning API v1 补丁。App 继续使用官方源码，后台使用相同的官方 Python 依赖环境，并用原生租约保留正在使用的依赖。源码视图独立固定提交，避免 App 替换检出目录时正在执行的复盘混用两个版本。学习配置、数据和队列独立。

```sh
scripts/bridge update prepare --official-source /absolute/path/hermes-agent --upgrade-official
scripts/bridge update apply
```

绑定后两种升级入口共存：

- **插件发起**。启用 `upgrade_official` 与 `auto` 后，插件先验证新稳定版和补丁，再通过 Hermes 原生 updater 升级官方基座。原生 updater 在选定提交时核对它与候选完全相同，频道目标发生变化便停止。当前自动发起支持已审查的 v0.23.0 原生 updater。后续版本需在适配时核对原生升级接口。
- **App 或官方 CLI 发起**。监督进程启动时检查本地官方基座，并每 30 秒检查官方源码或依赖发布是否变化。官方依赖就绪后在该环境启动候选，验证通过再切换学习服务。只检查本机可运行 `scripts/bridge update check --installed`，立即执行相应验证与排队可运行 `scripts/bridge update run --installed`。

官方更新和学习切换分别记录。App 已更新但补丁或学习流程尚待适配时，学习服务继续运行已验证的版本。原生 PM 若在服务停止期间回收旧依赖，恢复时可以用同一源码视图的已测试私有环境启动。旧版本保留作回退点。

本机已将 v0.23.0 学习服务切到官方依赖环境，并读回 worker 持有的原生租约。官方更新入口通过了目标一致与目标漂移的演练。本次官方版本已是 v0.23.0，未发生跨版本的 App 升级。见 [0.3.1 验收记录](../evidence/updates/v0.3.1.json)。

## 自进化机制重大变化：先商定方案

用户选择了自动验证后升级，同时要求：**新版自进化机制有较大变化、需要重新设计整套流程时，先提示并商定修改升级方案。** 自动更新策略不包含对这类重设计的预先授权。

未适配版本会比较原生 review、触发计数、记忆/技能工具、Curator、插件 API 和数据实现。关键文件移除/改名、较大的改动量或多个关键组件同时变化，会保守标记 `plan_required`。这是需要人工分析的信号，不是程序对语义破坏的最终判断。比较缺失、截断或较小但未审查的机制变化标记 `mechanism_review_required`，不能当成兼容通过。

遇到重设计，由维护技能说明：官方变化和证据、现有采集→触发→复盘→写入→维护→召回各环节的影响、建议替代流程、数据迁移及回退方案、验证边界。与用户商定后再改流程、补丁和适配记录。未商定时，旧学习服务照常运行，新版不进入构建与激活。

`patches/compatibility.json` 索引确切 tag/commit/补丁/文件哈希及必需回归；对应 manifest 的 `mechanism_review` 记录人工审查结论。小幅兼容变更使用 `compatible`；重设计完成后使用 `adapted`，并以 `agreed_plan` 引用实际商定的方案。缺失结论或 `redesign_required` 均不能准备或激活。不能根据版本号、补丁应用成功或单独的 release notes 声称兼容。

## 验证和切换

1. 只发现官方非预发布稳定 tag，按数字比较版本。网络失败保留当前环境并按周期重试。
2. 在学习 home 旁的私有 `updates/` 目录准备候选。核对官方提交、补丁及完整补丁文件哈希。原生 PM 从冻结锁文件构建测试环境，共用模式另外用官方安装的已发布依赖环境验证启动。
3. 运行 Learning API、阈值/空闲复盘、真实原生工具循环（本地脚本化 provider）、写入保护、暂存、技能锁、Curator、认证回退、外围桥接及 MCP 回归。验证前后源树和插件指纹必须一致。缺文件、测试失败或验证后代码变化都禁止激活。
4. 监督进程向旧 worker 请求完成当前复盘，不为升级强行取消复盘。hooks 在此期间继续写持久队列。
5. 旧 worker 完全退出后，以单 owner 锁保存学习数据快照及切换事务。修改 source/python 指针，候选以暂停学习状态启动，读回 API、状态和项目作用域 context；检查通过后才提交事务并恢复学习。
6. 新 worker 启动失败则停止候选、恢复快照和旧指针。监督进程崩溃留下的未提交事务在下次启动时恢复；已经提交的事务不回放旧快照覆盖新知识。

快照覆盖 Learning API v1、native review 和 Curator 使用的 `config.yaml`、`SOUL.md`、`memories/`、`skills/`、`learning/`、`sessions/`、`state/`、`.curator_backups/` 与 `state.db` 及其 WAL/SHM。新的机制如果增加存储，必须在适配时重新核对这个集合。恢复不覆盖 `bridge/` 事件箱，不复制或回放根账号认证；已有旧运行时保留。成功升级后的手动 rollback 用**当前**学习数据启动旧运行时，经暂停启动检查后启用，失败则返回原运行时，不丢弃升级后新学到的知识。

失败候选不会在同一插件指纹下每日反复构建/切换；修改适配器或显式 `prepare` 才重试。关闭自动策略会取消尚未开始的自动激活请求。更新检查、待商定方案、验证失败和切换/回退结果通过现有学习通知通道显示；详细状态保存在私有目录，MCP 不导出事务中的完整配置。

## 验证边界

合同测试、脚本化 provider、隔离 worker/MCP 检查证明所测接口和行为，不等于新版真实 provider 或实际 Codex App 全链路验收，更不代表学习质量提升。历史 v0.22.7 App/provider 证据保留原版本，不重标成当前版本。真实 App hooks 信任仍按原生 Codex 界面处理。

候选构建不使用生产学习数据或账号凭据。运行时切换后仍按现有 native auth fallback 使用原登录。通知是否被系统展示、长期真实学习和新 provider 行为需要相应运行证据，不能由启动状态替代。
