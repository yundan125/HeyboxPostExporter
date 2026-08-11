# HeyboxPostExporter

Windows 本地小黑盒帖子导出工具。它连接正在使用的 Microsoft Edge（日常 Profile），复用登录状态和已打开的目标帖子页，导出 HTML、Markdown、JSON 与图片。

## 使用方法

1. 正常打开 Microsoft Edge。
2. 在 Edge 打开 `edge://inspect`。
3. 开启 “Allow remote debugging for this browser instance”。
4. 启动 `HeyboxPostExporter.exe`。
5. Edge 询问授权时点击“允许”。
6. 输入小黑盒帖子链接并选择输出内容。
7. 点击“开始导出”。

支持普通帖子链接与含 `link_id` 的分享链接。默认输出到“文档\小黑盒帖子导出”，每次导出创建不覆盖的独立帖子目录。

## 浏览器连接

程序使用系统 Node/npx 启动固定版本：

```text
chrome-devtools-mcp@1.6.0 --autoConnect
```

连接对象始终是 Edge 日常 User Data。程序不会复制 Cookie、创建独立浏览器 Profile、关闭 Edge，也不使用 Playwright `connect_over_cdp()` 连接当前实例。退出程序时只结束自己启动的 MCP sidecar。

若目标帖子已经打开，程序按帖子 ID 直接复用并且不刷新；否则创建或复用一个 Heybox Exporter 工作标签页。其他网站和用户标签页不会被导航或关闭。

## 抓取与风控

程序读取网页自身产生的 `/bbs/app/link/tree` 和 `/bbs/app/comment/sub/comments` 响应，不重放历史 API，也不自行构造分页请求。评论保持网页热度顺序；API、DOM 与置顶结果按统一 comment ID 合并。

遇到验证码时，程序立即暂停所有自动页面动作，等待用户点击“我已完成验证”。遇到频率限制时不会定时重试，只有用户点击“重新尝试”才进行一次页面状态检查。抓取期间可点击“停止”，这不会关闭 Edge 或 sidecar。

## 导出内容

- 阅读版 HTML
- 易读 Markdown
- 包含 post、comments、replies、media、statistics、completeness、diagnostics 信息的 JSON
- 可选帖子图片和评论图片
- 可选 Comment Diagnostics：`debug/comment-report.txt`、`debug/comment-diagnostics.csv`、`debug/raw/`

图片下载失败会记录日志并继续生成其余文件。详细技术日志保存在 `logs/latest.log`。

## 开发与构建

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_browser_controller.py tests\test_exporters.py tests\test_request_control.py
.\build.ps1
```

稳定发布路径：

```text
D:\code\heybox\dist\HeyboxPostExporter.exe
```

这是可直接运行的 Windows x64 单文件版本。完整 onedir 版本仍保存在：

```text
D:\code\heybox\dist\HeyboxPostExporter\HeyboxPostExporter.exe
```
