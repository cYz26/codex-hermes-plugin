> 本文保留 0.2.0 试点记录。当前全局版本见 [0.4.2 运行与升级](./LINUX-GLOBAL-UPDATES.zh-CN.md)。

# Linux 0.2.0 试点运行与验收

Codex 执行业务，独立 Hermes v0.22.7＋Learning API v1 负责学习。当前机器的源码、解释器和数据均与现有个人 Hermes 分开。

## 当前进度

截至本次实施检查：原生相关回归 343 项通过，外围回归 36 项通过；真实模型探针完成 memory 写入及同一技能的创建、修订；systemd 实测通过启动、重复 owner 拒绝、强制退出自动重启及正常停止。

**真实 SSH hooks 与 A→B→C 验收仍待完成。** 服务在采集验证阶段保持停止；安装或模型探针通过不等于完整试点已经完成。机器可读进度见 [Linux 验收](../ACCEPTANCE.linux.json)，旧 [macOS 验收](../ACCEPTANCE.json) 保留其原版本结论。

## 初始化

使用独立解释器运行 `scripts/bridge init`，指定现存项目、固定源码和新 home：

```sh
/ABS/runtime/bin/python scripts/bridge init \
  --source /ABS/hermes-source --python /ABS/runtime/bin/python \
  --project /ABS/pilot-project --home /ABS/pilot \
  --provider custom:example --model YOUR_APPROVED_MODEL \
  --api-mode codex_responses --reasoning-effort max \
  --base-url http://127.0.0.1:17413/client-retry \
  --session-affinity-header x-session-id --dry-run
```

核对后去掉 `--dry-run`。示例 endpoint 是本机路由，不是可用于任意机器的公共地址。具名 provider 不创建 OpenAI OAuth profile。会话路由头由 Hermes 原生机制按会话生成，不能用一个静态身份代替。

初始化核对 Learning API 补丁的 14 个文件摘要。Git checkout 验证基线；官方 archive 安装另核对同级 `source-provenance.json` 中的固定 commit、下载 URL 和 archive checksum。构建使用原生 PM；archive 安装使用原生 install-stamp 记录固定基线及补丁后的 dirty 状态，不伪造 Git 提交。

## 服务和诊断

```sh
scripts/bridge systemd --settings /ABS/pilot/bridge.json
scripts/bridge systemd --settings /ABS/pilot/bridge.json --start
scripts/bridge doctor --settings /ABS/pilot/bridge.json --json
scripts/bridge status --settings /ABS/pilot/bridge.json
scripts/bridge stop --settings /ABS/pilot/bridge.json
```

`systemd` 默认只生成候选；`--start` 先调用本机 systemd parser 验证，再安装并启动 `hermes-learning-pilot.service`。已有定义必须显式 `--replace`；替换前保留前一份定义。服务使用私有权限、5 秒失败重启和 60 秒退出超时。

`doctor` 分别报告配置、IPC 可达性、快照时间／陈旧状态、覆盖缺口和该 home 的最近模型探针。`reachable` 只代表桥接能响应；不证明模型调用或业务学习通过。模型验证在独立验证 home 中运行，不能把它的经验复制进 pilot 冒充真实聊天学习。

```sh
systemctl --user status hermes-learning-pilot.service
journalctl --user -u hermes-learning-pilot.service -n 80
systemctl --user stop hermes-learning-pilot.service
systemctl --user disable hermes-learning-pilot.service
```

日志和完整探针回执放在 `evidence/private/linux-0.2.0/`，不发布凭据、完整对话或 live delivery token。配置、学习数据和回退快照不放入插件发布包。

## 模型探针

`probes/real_learning.py` 使用显式合成 CSV 规范，先验证 memory／skill 写入，再以 NFKC 反例验证技能修订。运行前确保该验证 home 没有其它 owner。

```sh
/ABS/runtime/bin/python probes/real_learning.py \
  --settings /ABS/validation/bridge.json \
  --output /ABS/private/run-unique.json --run-id unique-attempt
```

已有输出文件拒绝覆盖。遇到 unknown 或失败先读回执，不更换 ID 盲目重跑。业务侧采用证据必须来自真实 Codex 任务，不能用本探针替代。

## Codex 安装、信任和升级

插件通过本地 marketplace 正常安装，版本 0.2.0。用户层默认禁用学习插件，只有 pilot 的项目配置启用。项目的机器专用配置不进入 Git；原有宿主 hooks 的信任与行为保持宿主规则。

新 hook 必须检查定义并通过正常信任记录核对其当前 hash；不使用 `--dangerously-bypass-hook-trust`。安装、信任、实际执行和成功采集分别验收。

Codex 控制 socket 使用 WebSocket，不能向它直接发送按行 JSON。只读元数据查询使用 `initialize` 后的原生 API，并保留返回字段的版本差异。

0.166.0 的升级使用指定版本 CLI 的 `app-server daemon update --from-cli`。先等全部活跃任务进入空闲，保留 0.165.0 二进制和管理元数据，再切换并验证连接。若升级失败，在空闲条件下用旧 CLI 的同一流程恢复；不在活跃任务中重启服务。

## 最终验收

- 真实任务 A 产生 hooks，Hermes 自动写入经验。
- 新聊天 B 通过 MCP 加载相关版本，实际应用并验证；随后提出明确反例。
- Hermes 修订后，聊天 C 使用新版本验证反例。
- 断连／重启／模型路由失败有清晰状态和恢复依据，不重复写入或无限复盘。
- 非试点项目不被学习桥采集；现有个人数据保持独立。

全部通过后才将 Linux 验收标为 complete，并让 pilot 服务持续运行。当前研究、探针与历史 macOS 证据不能补齐未执行的条目。
