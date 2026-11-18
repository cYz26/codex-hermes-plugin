# Hermes Learning 0.11.0

0.11.0 汇总 `0.10.2` 之后的共享学习、Hermes `v0.23.2` 兼容适配、回合终态恢复、精确版本发布审查与只读日审能力，并完成云端 Linux 开发机与桌面接收器维护适配。

## 主要更新

- **Codex 与原生 Hermes 共享学习及 v0.23.2 适配**：支持绑定独立的本地 Hermes 执行 profile，通过同一学习服务串行维护项目记忆、用户记忆、技能发布、待审队列与历史检索（覆盖 **Hermes v0.23.1** 与 **v0.23.2**，见 [共享学习说明](./SHARED-LEARNING.zh-CN.md) 与 [兼容清单](../patches/compatibility.json)）。
- **企业专用环境下的共享边界**：在启用 `trusted_enterprise` 的云端开发机上支持共享学习，强制核对主机、UID 与模型配置摘要，拦截 Cookie／Authorization／私钥／JWT 与显式 L4 禁学标记；边界失效或停用企业模式后自动隐藏企业共享记忆与技能。
- **企业边界跨版本 schema 迁移与用户级 PATH 适配**：原生 Hermes 升级仅推进 `_config_version` 且模型／provider 配置完全未变时自动同步边界摘要，模型或路由变更仍立即拦截；systemd 用户服务默认包含 `~/.local/bin`，使无 root 安装的 `bwrap` 隔离校验与 `codex` 自动适配在后台服务内正常可用。
- **云端开发机代理、限流恢复与原生更新重入保护**：新增可预览的 `scripts/bridge network-config` 持久化无凭据代理配置；修复 GitHub API 403 限流回退跳转未走配置代理及 `NO_PROXY` 绕过的问题；为远程 tag 解析与浅克隆增加 `HTTP/1.1`、有界重试和本地源码缓存；官方基座升级在匿名 API 限流时通过稳定版跳转与远端 tag 提交双重校验完成核对，并在 `hermes_bootstrap` 重入时保留精确提交守卫。
- **分版本 site 适配、受管 CLI 绑定与平滑网关切换**：支持在 `site-migration/releases/<tag>/migration.json` 为新版预置封存 site 补丁，保留当前运行版本的 `current-site.json`；存在 `site_manifest` 时官方基座升级使用 `--no-gateway-restart` 避免提前中断网关，待候选验证与提交完成后按空闲排空切换；受管启动器跟随最新已安装插件并将原生 CLI 子命令绑定在已验证的依赖环境上。
- **共享宿主工作区归属与桌面接收器兼容**：全局项目定位忽略带 sticky bit 的公共目录（如 `/tmp`）及非当前用户持有的祖先目录或 `.git` 标记，防止共享开发机上的残留 `/tmp/.git` 将独立目录合并到同一项目作用域；SSH 桌面通知接收器兼容 macOS 系统自带 Python 3.9 并捕获本地 SQLite 暂态异常。
- **快照完整性与原生验证**：升级切换快照纳入 `pending/`、`shared-knowledge/` 及发布审查元数据；`scripts/verify_native.py` 与 CI 默认覆盖 `v0.23.2` 及其共享学习合同探针。
