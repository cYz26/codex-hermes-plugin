# Hermes Learning 图标：学习回路

左下是 Codex，右上是 Hermes。两段弧形箭头分别表示把任务经验交给 Hermes 学习，以及把学到的经验带回后续 Codex 任务。深靛蓝底让两枚标识在浅色和深色界面上都能看清。

- [插件使用的 PNG](../../../assets/hermes-learning-icon.png)：256 × 256，圆角外透明。插件清单的 `logo` 和 `composerIcon` 引用同一份文件。
- [蓝色项目图标](../../../assets/hermes-project-notification.png)用弧形箭头连接 Codex 和 Hermes，右下的文件夹表示项目范围。[紫色自进化图标](../../../assets/hermes-global-notification.png)用于全局共享和系统版本检查及更新，右下的小地球表示跨项目范围。[项目 SVG](../../../assets/hermes-project-notification.svg)和[全局 SVG](../../../assets/hermes-global-notification.svg)复用本目录源稿中的品牌标识，使用同源浏览器渲染为 256 × 256 PNG。
- macOS 安装器用系统 `sips`／`iconutil` 从各自的 PNG 创建 16–1024px 和 Retina 尺寸，再放入 App 的 `Contents/Resources/`。文件名带内容摘要，由 `CFBundleIconFile` 指定。
- [SVG 源稿](./hermes-learning-icon.svg)：底板、箭头和 Hermes 头像是矢量；Codex 云形标识内嵌原始 PNG。

Hermes 头像取自 [Nous Research 官方仓库的白色 Nous Girl](https://github.com/NousResearch/hermes-agent/blob/1fa5fea74f69566196707c7039c35c26155cfb9d/assets/nous-girl-white.svg)。Codex 云形标识取自 [OpenAI 官方 Codex 扩展](https://marketplace.visualstudio.com/items?itemName=OpenAI.chatgpt) `26.1007.21434` 的 `webview/assets/codex-app-ga-logo-3e5209898ca3.png`。两枚标识均按原比例排版，归各自品牌所有。
