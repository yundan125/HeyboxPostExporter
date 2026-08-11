# HeyboxPostExporter

HeyboxPostExporter 是一个面向 Windows 的小黑盒帖子完整导出工具。它连接你日常使用的 Microsoft Edge，复用现有登录状态和已经打开的帖子页面，将原帖、一级评论、楼中楼回复及图片保存为 HTML、Markdown 和 JSON。

程序不会复制 Cookie、创建独立浏览器 Profile，也不会关闭或重新启动 Edge。浏览器操作集中通过固定版本的 `chrome-devtools-mcp` 完成。

## 功能

- 导出帖子标题、作者、UID、发布时间、正文、点赞、收藏和原帖链接。
- 导出一级评论、楼中楼回复、楼层、时间、点赞、IP 属地、楼主和置顶标记。
- 保持小黑盒页面原有的热度顺序，不按照楼层重新排序。
- 合并 API、DOM、分页和置顶评论，并按统一的 comment ID 去重。
- 下载帖子图片、评论图片和回复图片到本地。
- 生成阅读版 HTML、易读 Markdown 和完整 JSON 数据。
- 支持可选的 Comment Diagnostics 评论诊断报告。
- 支持 CAPTCHA 手工恢复、频率限制暂停和用户主动停止。
- 支持普通帖子链接和带有 `link_id` 的分享链接。
- 同时提供 Windows x64 单文件版和完整 onedir 版。

## 下载

推荐下载单文件版本：

- [下载最新版 HeyboxPostExporter.exe](https://github.com/yundan125/HeyboxPostExporter/releases/latest/download/HeyboxPostExporter.exe)
- [查看全部 Releases](https://github.com/yundan125/HeyboxPostExporter/releases)

下载后可以直接运行，不需要安装 Python。单文件第一次启动会先解压自身运行环境，因此可能比后续启动稍慢。

GitHub Release 中的 `HeyboxPostExporter.exe` 是推荐的正式发布物。仓库内 `dist/` 目录用于保留可复现构建产物，不建议通过 Raw 链接作为日常下载入口。

仓库还提供完整 onedir 版本和压缩包：

- [`dist/HeyboxPostExporter/`](dist/HeyboxPostExporter/)
- [`dist/HeyboxPostExporter_Windows_x64.zip`](dist/HeyboxPostExporter_Windows_x64.zip)

> 程序目前没有代码签名。Windows SmartScreen 可能显示“Windows 已保护你的电脑”，请确认文件来自本仓库后再选择是否运行。

## 运行环境

- Windows 10 或 Windows 11 x64。
- Microsoft Edge。
- 系统已安装 Node.js 和 npx，并且可以从 `PATH` 中找到。
- 能够访问 npm，以便首次初始化浏览器连接组件。

当前正式版本固定使用：

```text
chrome-devtools-mcp@1.6.0 --autoConnect
```

开发与真实验证环境使用 Node.js `v24.12.0`、npm/npx `11.7.0`。程序不会把 Node.js 打包进 EXE；没有检测到 Node/npx 时，GUI 会显示安装提示，而不是直接崩溃。

## 快速开始

1. 正常启动你平时使用的 Microsoft Edge。
2. 在地址栏打开 `edge://inspect`。
3. 开启 **Allow remote debugging for this browser instance**。
4. 运行 `HeyboxPostExporter.exe`。
5. 如果 Edge 询问是否允许远程调试，请手工点击“允许”。
6. 等待 GUI 显示 Edge、授权和 Browser sidecar 均已连接。
7. 输入小黑盒帖子链接。
8. 选择导出格式、图片和诊断选项。
9. 选择输出目录，然后点击“开始导出”。

程序默认把文件保存到：

```text
%USERPROFILE%\Documents\小黑盒帖子导出
```

每次导出都会创建新的帖子目录。同名目录依次使用 `_2`、`_3` 等后缀，不会直接覆盖以前的结果。

## 支持的链接

普通帖子链接：

```text
https://www.xiaoheihe.cn/app/bbs/link/187649012
```

含 `link_id` 的网页分享链接：

```text
https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?...&link_id=187649012
```

无法识别域名或帖子 ID 时，程序会在启动浏览器抓取前直接提示“无法识别小黑盒帖子链接”。

## 浏览器连接方式

正常工作流如下：

```text
日常 Microsoft Edge Profile
        ↓
edge://inspect 开启当前实例远程调试
        ↓
用户手工允许 Edge 授权
        ↓
chrome-devtools-mcp@1.6.0 --autoConnect
        ↓
HeyboxPostExporter
```

程序使用 Edge 的日常 User Data 目录，一般为：

```text
%LOCALAPPDATA%\Microsoft\Edge\User Data
```

如果目标帖子已经在 Edge 中打开，程序会按帖子 ID 复用该页面，不刷新、不重复打开。如果不存在目标页面，程序创建或复用一个 Heybox Exporter 工作标签页。ChatGPT、其他网站和用户自己的其他 Edge 标签页不会被导航或关闭。

程序退出时只结束自己启动的 MCP sidecar，不会调用 `browser.close()`，也不会终止 `msedge.exe`。重复点击“重新连接”时复用健康 sidecar；断线时最多尝试一次合理重连。

## 数据获取方式

程序优先读取小黑盒页面自身产生的结构化响应：

```text
/bbs/app/link/tree
/bbs/app/comment/sub/comments
```

完整流程为：

```text
网页正常加载、滚动或点击官方控件
        ↓
网页自身产生 fetch/XHR 请求
        ↓
MCP Network 读取已经产生的响应体
        ↓
现有 API parser 和 CommentCollector 解析、去重、归并
        ↓
最终 DOM 只作为展示字段和可见评论的补充
```

程序不会捕获一个历史 URL 后自行 `fetch` 重放，也不会构造接口分页参数。一级评论通过页面官方加载行为持续获取，直到 `has_more_floors=false` 或页面无法再产生新数据；楼中楼通过官方 `.comment-children__load-all` 控件触发，结合 `root_comment_id`、`lastval`、`has_more` 和 `child_num` 判断完成状态。

如果已有页面的历史 Network Response 已不可用，程序会保留当前页面、不刷新，并使用已经渲染的 DOM 作为降级数据源。

## CAPTCHA 与频率限制

检测到验证码、安全验证或 API `show_captcha` 后，程序进入 `CAPTCHA_REQUIRED`：

- 立即停止导航、点击、滚动、执行脚本、展开回复和重试。
- 不轮询页面，不自动刷新。
- 用户需要在当前 Edge 工作页手工完成验证。
- 点击“我已完成验证”后只检查一次当前页面状态。

检测到 HTTP 429、“操作过于频繁”或“请稍后再试”后，程序进入 `RATE_LIMITED`：

- 停止全部自动页面动作。
- 不设置定时重试。
- 用户等待一段时间后点击“重新尝试”，程序只检查一次。

抓取期间点击“停止”会取消后续浏览器动作，但不会关闭 Edge 或仍然健康的 sidecar。

## 导出选项

GUI 默认启用：

- 下载帖子图片到本地。
- 下载评论和回复图片到本地。
- 导出 Markdown。
- 导出 HTML。
- 导出 JSON。

Comment Diagnostics 默认关闭。启用后会额外保存评论来源、分页、去重和完整性诊断。

图片下载失败不会导致整个帖子导出失败；失败 URL 会写入日志，其余数据继续生成。

## 输出目录结构

```text
输出目录\
  帖子标题\
    帖子标题.html
    帖子标题.md
    帖子标题.json
    assets\
      post\
      comments\
    debug\                         # 仅启用 Comment Diagnostics 时生成
      comment-report.txt
      comment-diagnostics.csv
      raw\
```

Windows 文件名非法字符 `\ / : * ? " < > |` 会被自动处理。

### HTML

HTML 使用适合本地阅读的样式，包含帖子信息、正文、图片、评论、楼中楼、时间、点赞、IP 属地、楼主和置顶标记。

### Markdown

Markdown 以标题、原帖、一级评论和缩进回复组织内容，适合编辑、搜索和长期保存。

### JSON

JSON 是完整结构化数据源，主要字段包括：

```text
post
comments
replies
media
statistics
completeness
diagnostics
source
```

`completeness` 常见状态：

- `complete_visible`：网页和当前账号可读取的内容已经加载完成。
- `partial`：仍有分页或页面交互没有明确完成。
- `counted_but_not_returned` 会记录在统计和诊断中：官方计数存在，但服务器没有返回可导出的 comment ID 或正文，程序不会伪造内容。

## 日志

GUI 只显示面向普通用户的关键进度，例如：

```text
已连接 Microsoft Edge
找到目标帖子
正在获取原帖
正在加载评论
正在展开回复
正在下载图片
正在生成 HTML
导出完成
```

详细技术日志保存在程序目录下的：

```text
logs\latest.log
```

单文件版会把设置和日志写在 EXE 所在目录。请把 EXE 放在当前用户拥有写权限的位置运行。

## 常见问题

### 开始导出按钮不可用

请依次检查：

1. Edge 是否正在运行。
2. `edge://inspect` 中是否开启当前实例远程调试。
3. Edge 授权是否已手工允许。
4. Browser sidecar 是否显示已连接。
5. 帖子链接能否识别。
6. 输出目录是否有效。

### 未检测到 Node.js

安装 Node.js 后重新启动程序，并在 PowerShell 中确认：

```powershell
node --version
npx --version
```

### 首次连接等待较久

首次运行 `npx` 可能需要下载 `chrome-devtools-mcp@1.6.0`。请保持网络可用，不要连续启动多个程序实例。

### Edge 一直等待授权

打开 `edge://inspect`，确认已开启远程调试，并在 Edge 弹出的授权提示中点击“允许”。如果曾拒绝授权，请在 GUI 点击“重新连接”。

### 帖子页面被关闭

抓取会停止并提示“帖子页面已被关闭”。重新打开目标帖子或重新开始导出即可，程序不会随机操作其他页面。

### 官方评论数和导出数量不同

官方统计、API 返回和 DOM 可见内容可能不同。置顶评论可能不计入普通一级评论数；被隐藏或删除的评论也可能只有计数而没有正文。请启用 Comment Diagnostics 查看详细归并和缺口说明。

## 从源码运行

需要 Python 3.11 或更高版本。Windows PowerShell 示例：

```powershell
git clone https://github.com/yundan125/HeyboxPostExporter.git
cd HeyboxPostExporter

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m heybox_exporter
```

也可以以可编辑模式安装命令行入口：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
heybox-export-gui
```

### 命令行导出

```powershell
heybox-export "https://www.xiaoheihe.cn/app/bbs/link/187649012" -o exports
```

可用参数：

```text
--mhtml PATH             离线解析 MHTML，不访问网站
-o, --output PATH        指定导出父目录
--edge-executable PATH   手工指定 msedge.exe
--debug                  保存评论诊断和原始响应
--no-post-images         不下载帖子图片
--no-comment-images      不下载评论图片
--no-markdown            不生成 Markdown
--no-html                不生成 HTML
--no-json                不生成 JSON
```

离线 MHTML 示例：

```powershell
heybox-export --mhtml ".\网上经常有说黑猴后期五六章赶工.mhtml" -o exports
```

## 测试

安装开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

运行项目测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

常用的针对性检查：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests\test_browser_controller.py `
  tests\test_comment_collector.py `
  tests\test_exporters.py `
  tests\test_request_control.py
```

## 构建 Windows x64 程序

使用 64 位 Python，在项目根目录执行：

```powershell
.\build.ps1
```

构建脚本会：

1. 检查或创建 `.venv`。
2. 从 `requirements-dev.txt` 安装依赖。
3. 验证 Python 是 64 位运行时。
4. 构建完整 onedir 版本。
5. 构建 PyInstaller `--onefile` 单文件版本。
6. 使用由原始 `app-icon-source.png` 居中裁切得到的 `app-icon.png` 设置 EXE、任务栏和窗口图标。
7. 排除正常 MCP 工作流不需要的 Playwright 运行时，并生成 onedir 压缩包。

构建结果：

```text
dist\HeyboxPostExporter.exe                         # 推荐：Windows x64 单文件
dist\HeyboxPostExporter\HeyboxPostExporter.exe    # 完整 onedir 版本
dist\HeyboxPostExporter_Windows_x64.zip            # 完整 onedir 压缩包
```

单文件版本仍需要系统 Node/npx；构建脚本不会打包 Node.js，也不会修改系统代理或用户的全局 npm 配置。

## 项目结构

```text
src/heybox_exporter/
  browser_controller.py    MCP sidecar 与浏览器操作统一接口
  mcp_client.py             MCP JSON-RPC/stdio 客户端
  mcp_collector.py          页面选择、Network 捕获和完整加载流程
  api_parser.py             小黑盒结构化响应解析
  collector.py              评论去重、归并与完整性统计
  dom_parser.py             DOM/MHTML 解析与补充
  request_control.py        CAPTCHA、限流、停止和 Network Gate
  assets.py                 图片归档
  exporter/                 HTML、Markdown、JSON 输出
  gui/app.py                Tkinter 图形界面

tests/                      针对性测试
docs/                       分析文档
build.ps1                   Windows x64 构建脚本
dist/                       已构建发布程序
```

## 隐私与行为边界

- 所有帖子数据和导出文件都保存在本机。
- 程序没有遥测、云端账户或数据库。
- 不复制或导出 Edge Cookie。
- 不关闭、重启或强制结束 Edge。
- 不操作非小黑盒工作页。
- 不主动重放小黑盒 API 请求。
- 不会为了匹配官方计数而伪造、删除或补写评论正文。

## 已知限制

- 依赖小黑盒当前网页结构和接口字段，网站更新后可能需要同步适配。
- CAPTCHA 必须由用户在 Edge 中手工完成。
- 服务端没有返回正文的隐藏或删除评论无法恢复。
- 未签名单文件可能触发 SmartScreen 提示。
- 单文件版启动时需要解压运行环境，并需要 EXE 所在目录可写以保存设置和日志。
