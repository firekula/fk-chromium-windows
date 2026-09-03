# FK Chromium Windows x64 设计

## 目标

基于 `ungoogled-chromium` 构建 Windows x64 浏览器发行版，产品英文名为 **FK Chromium**，中文介绍名为 **火焰库拉浏览器**。项目需要在公开 GitHub 仓库中使用免费的 GitHub 托管 Runner 自动检测上游稳定版、分段编译并发布未签名的安装程序与便携包。

首版只处理品牌定制和自动发布，不增加浏览器功能，不预装扩展，不改变默认主页、搜索引擎或隐私设置。

## 仓库结构

项目使用两个公开仓库：

- `firekula/fk-chromium`：基于通用 `ungoogled-chromium` 仓库，保存通用源码补丁和品牌资源。
- `firekula/fk-chromium-windows`：基于 `ungoogled-chromium-windows`，保存 Windows 构建、打包、自动更新和发布逻辑，并通过子模块引用 `firekula/fk-chromium`。

采用补丁叠加方式，不托管完整 Chromium 源码。构建时下载固定版本的上游源码，依次应用通用补丁、Windows 补丁和 FK Chromium 品牌补丁。

## 产品标识

用户可见的产品名称统一为 `FK Chromium`，中文文档、发布说明和项目介绍使用 `火焰库拉浏览器`。首版品牌替换覆盖：

- 浏览器窗口、开始菜单、桌面快捷方式、任务栏和“关于”页面；
- Windows 安装程序及“应用和功能”卸载列表；
- 应用图标、安装程序图标和快捷方式图标；
- 产品说明、版权信息以及崩溃或错误对话框中的产品名称；
- 安装包、便携包和校验文件的文件名。

内部主程序继续使用 `chrome.exe`，避免破坏 Chromium 对进程、沙箱、崩溃处理和安装程序文件名的内部依赖。用户可见入口均显示 `FK Chromium`。

FK Chromium 必须使用独立于 Chrome、Chromium 和 ungoogled-chromium 的以下标识：

- 安装目录与用户数据目录；
- Windows 注册表安装和卸载键；
- AppUserModelID；
- 安装模式标识及应用 GUID；
- 快捷方式名称。

这些标识在首次实现时生成并固定，后续版本不得更换，以保证升级安装和用户数据连续性。

## 图标与品牌资源

采用已确认的第一版图标：橙红色多层火焰轮廓、深蓝色内部火焰和白色 `FK` 字标。该图标不得改为第二版简化造型。

最终资源需要从选定图像制作：

- 透明背景主图；
- 包含 16、20、24、32、40、48、64、128、256 像素图层的 Windows `.ico`；
- Chromium 资源系统需要的各尺寸 PNG；
- 带 `FK Chromium` 和 `火焰库拉浏览器` 的横版品牌标识。

处理时允许清理透明背景和缩放锐化，但不得重新设计火焰轮廓、配色或 `FK` 字形。

## 构建与打包

目标平台仅为 Windows x64。删除或禁用 x86、ARM64 和 Winget 发布任务，避免无关构建。

构建基于官方 `ungoogled-chromium-windows` 的免费 Runner 分段方案：

1. 首段下载 Chromium、准备依赖、应用补丁并开始编译。
2. 每段接近 GitHub 单任务超时前安全停止 Ninja。
3. 压缩并上传构建树作为短期中间 Artifact。
4. 下一段在新的 `windows-2022` Runner 上恢复 Artifact 并继续编译。
5. 最多执行 12 段；提前完成时，后续阶段直接跳过。
6. 编译结束后运行打包脚本，生成安装程序和便携 ZIP。

发布文件名为：

- `FK-Chromium-<chromium-version>-Windows-x64-Installer.exe`
- `FK-Chromium-<chromium-version>-Windows-x64-Portable.zip`
- `SHA256SUMS.txt`

首版不进行 Windows 代码签名。README 和每次 Release 说明必须提示安装程序可能触发 Microsoft Defender SmartScreen 警告，且不得暗示文件已经签名。

## 上游检测与自动发布

定时工作流每天运行一次，读取官方 `ungoogled-chromium-windows` 的最新稳定标签，并与 FK Chromium 已发布版本记录比较。

发现新版本时：

1. 创建该版本的临时更新分支。
2. 更新 Windows 上游基线和通用源码引用。
3. 验证补丁列表、品牌补丁和资源输入。
4. 启动 Windows x64 分段构建。
5. 运行发布前验证。
6. 验证成功后创建 FK Chromium 标签及 GitHub Release。
7. 上传安装程序、便携包和 SHA-256 校验文件。

标签使用与上游版本一一对应且可排序的格式：`<chromium-version>-fk.<revision>`，首个品牌构建的 `<revision>` 为 `1`。同一 Chromium 版本因品牌补丁或打包修复而重发时，通过独立的 `rerelease` 人工入口分配高水位之后下一个无冲突编号；失败重试继续通过 `force_rebuild` 复用原预留编号，两者不得同时启用。

定时检测、手动运行和 Git 标签构建共享同一个可复用构建工作流，避免三套逻辑产生差异。仓库保留手动输入上游标签和“强制重新构建”的入口。

## 失败处理

任何失败都不得创建正式 Release。

- 上游无新版本：正常结束，不创建分支、Issue 或 Artifact。
- 同一版本已成功发布：跳过，不重复构建。
- 同一版本已自动尝试并失败：后续定时检测跳过，等待人工修复或手动强制重试。
- 补丁无法应用：停止在编译前，创建或更新该版本唯一的失败 Issue。
- 分段构建超时：上传中间状态并进入下一段，不视为失败。
- 编译、打包或验证失败：保留日志链接，创建或更新失败 Issue，不发布。
- GitHub Artifact 上传或下载偶发失败：在当前阶段有限重试；仍失败则终止。
- 超过 12 段仍未完成：标记失败并提示需要优化编译参数或改用更强 Runner。

失败 Issue 标题包含上游版本，正文记录失败阶段、工作流运行链接、提交 SHA 和人工重试方式。同一版本重复失败时更新原 Issue，避免产生重复 Issue。

## 验证标准

### 静态验证

- 品牌补丁能无交互应用到固定的上游源码版本。
- GN 生成成功，品牌相关参数未被忽略。
- 构建产物中不再出现用户可见的 `ungoogled-chromium` 产品名。
- 安装包、便携包和 `SHA256SUMS.txt` 名称与内容一致。
- 所有发布文件的 SHA-256 能重新计算并匹配。

### Windows 冒烟测试

- 安装程序可启动并完成当前用户安装。
- 开始菜单、桌面快捷方式、任务栏和卸载列表显示 `FK Chromium` 与选定图标。
- 浏览器能启动、打开本地页面并创建新标签页。
- “关于”页面显示 FK Chromium 品牌和正确 Chromium 版本。
- 用户数据写入 FK Chromium 独立目录，不读取或覆盖其他 Chromium 系浏览器数据。
- 卸载程序能启动并移除应用文件，不删除用户未选择删除的个人数据。
- 便携包解压后可直接启动，且包含运行所需文件。

GitHub 托管 Runner 不适合完成需要桌面交互的全面 UI 测试，因此自动发布前执行可脚本化的进程启动、版本输出、文件存在性、资源和目录检查；首次正式发布还需要在真实 Windows 10 或 Windows 11 x64 机器上人工验收安装、升级和卸载流程。

## 安全与发布权限

工作流权限默认只读。只有发布任务获得 `contents: write`，只有失败通知任务获得 `issues: write`。第三方 Actions 固定到明确版本；不在日志中输出令牌或其他 Secret。

自动发布只接受官方上游仓库的稳定标签，不从 Pull Request、任意 Fork 或未经验证的分支构建正式 Release。每个 Release 记录上游标签、上游提交、FK Chromium 提交和构建工作流链接，以便追溯。

## 非目标

首版不包括：

- x86、ARM64、Linux、Android 或 macOS 构建；
- 浏览器自动更新客户端；
- Microsoft Store 或 Winget 发布；
- Windows 代码签名；
- 新浏览器功能、预装扩展、主页或搜索引擎定制；
- 对 Chromium 完整源码的长期镜像。

## 完成定义

两个仓库包含可审查的品牌补丁、资源和自动化配置；手动触发一次 Windows x64 构建能够完成分段编译，自动创建包含安装程序、便携包和 SHA-256 文件的 GitHub Release；在真实 Windows x64 机器完成首次人工安装、启动、独立数据目录和卸载验收后，首版视为完成。
