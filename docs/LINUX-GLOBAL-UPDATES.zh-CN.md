# Linux／SSH 全局学习与更新（0.10.1）

主分支的 Linux 方案提供独立原生学习实例、systemd 用户服务、固定流程采集、检查后的共享、候选适配和 SSH 桌面通知。Codex 执行任务，Hermes 串行学习；通用实现与状态语义见 [实现整理](./SELF-EVOLUTION.zh-CN.md)。

新版语义准入、主动复盘、留存与不可变安装流程见 [云端核心合同](./CLOUD-0.5.0.zh-CN.md)；企业正文学习见 [企业专用部署](./ENTERPRISE-LEARNING.zh-CN.md)。

## 初始化与全局采集

需要 Python 3.11 或更高版本和已核对固定补丁的原生 Hermes 源码／解释器。客户端遇到缺少 SQLite 或 tomllib 的 PATH Python 时，在 Linux 尝试系统 `/usr/bin/python3`；该解释器也必须满足版本与标准库要求，worker 仍使用配置中验证过的解释器。

```sh
/ABS/runtime/bin/python scripts/bridge init \
  --source /ABS/patched-hermes-source --python /ABS/runtime/bin/python \
  --project /ABS/project --home /ABS/pilot --capture-scope global \
  --provider custom:example --model YOUR_APPROVED_MODEL \
  --api-mode codex_responses --reasoning-effort max \
  --base-url https://provider.example.invalid/v1 \
  --session-affinity-header x-session-id --dry-run
```

替换示例 provider／模型／地址，核对原生路由支持的 API 模式后，使用相同参数去掉 `--dry-run`。具名 provider 不创建 OAuth profile；默认 `openai-codex` 则通过原生隐藏 profile 管理认证。新建全局配置默认开启资料准入、固定流程投影及共享检查；旧配置需要显式选择迁移策略。

hooks 使用真实 cwd，Git 根目录／worktree 或非 Git 工作目录保留来源归属。只处理正常安装并信任的 hooks，不导入旧聊天。正文未准入时，默认仅保存固定动作与结果；详情见 [流程学习](./WORKFLOW-METADATA.zh-CN.md)。新包必须按宿主规则核对当前 hooks 信任，旧包指纹不代表新包已授权。

## systemd 与诊断

```sh
scripts/bridge systemd --settings /ABS/pilot/bridge.json --dry-run
scripts/bridge systemd --settings /ABS/pilot/bridge.json
scripts/bridge systemd --settings /ABS/pilot/bridge.json --start
scripts/bridge doctor --settings /ABS/pilot/bridge.json --json
scripts/bridge status --settings /ABS/pilot/bridge.json
systemctl --user status hermes-learning-pilot.service
journalctl --user -u hermes-learning-pilot.service -n 80
```

`systemd` 默认生成候选定义，`--dry-run` 只输出定义；`--start` 仅在 Linux 上执行，先使用 systemd parser 验证，再写入用户服务并启动。已有定义需要 `--replace`，替换前保存前一份定义。服务使用私有权限、5 秒失败重启与 60 秒退出超时。工作区、解释器与服务参数均使用显式路径。

`doctor` 分别报告配置、provider 配置／认证、IPC、陈旧快照、采集缺口和已有模型探针。可达性不等于模型验证通过。worker 不可用时，监督进程继续保留更新与恢复入口；hooks 不等待模型调用。

## 自动适配与升级

```sh
scripts/bridge update status --settings /ABS/pilot/bridge.json
scripts/bridge update check --settings /ABS/pilot/bridge.json
scripts/bridge update adapt --settings /ABS/pilot/bridge.json --version vX.Y.Z
```

更新策略在 `bridge.json` 中配置：

```json
{
  "updates": {
    "mode": "auto",
    "interval_seconds": 86400,
    "auto_adapt": true,
    "adaptation_timeout": 1800,
    "official_source": "/ABS/official-source",
    "upgrade_official": true,
    "site_manifest": "/ABS/site-migration/migration.json"
  }
}
```

`auto_adapt`、`upgrade_official` 和 `site_manifest` 是按部署需要显式开启的选项，新配置不会自动启用兼容维护或更改既有官方安装。默认按日发现稳定版，已绑定安装的本地变化按 30 秒检查；API 限流时使用官方 latest-stable 跳转和 Git tag 作有界替代，仍核对精确提交。

维护作业在私有候选中读取完整 Git 差异，只能维护该版本的兼容材料，不能修改旧版本、原生测试、验证器或全局配置。未修改的更新器重新核对目标、原生 updater 合同、补丁、原生／外围测试及运行时。每个目标／控制器指纹最多两轮；中断为 unknown，显式 `--retry` 保留旧尝试记录。重设计或不可逆迁移进入 `plan_required`，先形成具体方案。

## 本机适配与回退

`migration.prepare` 把原安装的改动合并到干净官方副本，保留原树并记录冲突／遗漏；`seal` 将人工解决理由绑定文件摘要，封存不等于测试通过。site 适配保留固定回归集合，provider、模型、推理档位、输出预算、fallback 和 gateway 行为属于验证边界，机器配置与凭据不进入仓库。

官方安装、site 适配层和学习 worker 分别验证。监督进程等待复盘结束后，暂停学习启动候选，核对实际解释器、API 和召回再提交。提交前失败恢复原生数据和派生来源／发布／流程凭证索引；事件箱、交付与通知队列、根账号认证保留当前状态。提交后的回退用当前数据检查旧运行时，不覆盖新知识。

```sh
scripts/bridge update rollback --settings /ABS/pilot/bridge.json
systemctl --user stop hermes-learning-pilot.service
systemctl --user disable hermes-learning-pilot.service
```

`update rollback` 提交回退请求，由监督进程处理；服务的正常停止与禁用和运行时回退是独立操作。

## 验证

```sh
python3 -B scripts/check_release.py
```

原云端分支的验证见 [0.4.2 基线记录](../evidence/releases/cloud-0.4.2.json)。[历史全局报告](../ACCEPTANCE.linux-global.json) 保留整理前的 0.4.2／0.4.1 验收范围，[0.2.0 试点](./LINUX-OPERATIONS.zh-CN.md) 保留当时未完成项；私有原始报告未替换成新的完成声明。真实 provider、App 跨聊天应用与 Mac 横幅／点击需要独立验收，通知部署见 [SSH 桌面通知](./SSH-NOTIFICATIONS.zh-CN.md)。
