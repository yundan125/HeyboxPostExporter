from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from ..browser_controller import (
    ChromeDevToolsMcpController,
    find_edge_executable,
    inspect_edge_environment,
    normal_edge_user_data_dir,
)
from ..logging_setup import create_logger
from ..request_control import RequestControl, RequestState
from ..service import TaskOptions, application_dir, run_export_with_logger
from ..url_parser import parse_post_url


STATUS_COLORS = {
    "green": "#16803c",
    "yellow": "#a15c00",
    "red": "#b42318",
    "gray": "#667085",
}

APP_ICON_NAME = "app-icon.png"


def bundled_resource_path(name: str) -> Path:
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return Path(bundle_dir) / name
    return Path(__file__).resolve().parents[3] / name


class ExporterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self._app_icon: tk.PhotoImage | None = None
        icon_path = bundled_resource_path(APP_ICON_NAME)
        if icon_path.is_file():
            self._app_icon = tk.PhotoImage(file=icon_path)
            self.iconphoto(True, self._app_icon)
        self.title("小黑盒帖子完整导出工具")
        self.geometry("900x850")
        self.minsize(790, 720)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.browser_worker: threading.Thread | None = None
        self.browser_controller: ChromeDevToolsMcpController | None = None
        self.closing = False
        self.request_control: RequestControl | None = None
        self.base_dir = application_dir()
        self.settings_path = self.base_dir / "heybox-settings.json"
        self.settings = self._load_settings()
        configured_edge = self.settings.get("edge_executable")
        self.edge_executable = find_edge_executable(str(configured_edge)) if configured_edge else find_edge_executable()
        self.edge_ready = False
        self.logger = create_logger(self.base_dir, lambda text: self.events.put(("log", text)))
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_events)
        self.after(350, self._initialize_browser)

    def _load_settings(self) -> dict[str, object]:
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_settings(self) -> None:
        payload = {
            "edge_executable": str(self.edge_executable) if self.edge_executable else "",
        }
        try:
            self.settings_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            self.logger.warning("无法保存设置：%s", error)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(7, weight=1)

        ttk.Label(root, text="小黑盒帖子完整导出", font=("Microsoft YaHei UI", 17, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 16)
        )
        ttk.Label(root, text="小黑盒帖子链接：").grid(row=1, column=0, sticky="w", pady=6)
        self.url = tk.StringVar()
        self.url.trace_add("write", lambda *_: self._update_start_state())
        ttk.Entry(root, textvariable=self.url).grid(row=1, column=1, columnspan=2, sticky="ew", pady=6)

        ttk.Label(root, text="输出目录：").grid(row=2, column=0, sticky="w", pady=6)
        self.output = tk.StringVar(value=str((Path.home() / "Documents" / "小黑盒帖子导出").resolve()))
        self.output.trace_add("write", lambda *_: self._update_start_state())
        ttk.Entry(root, textvariable=self.output).grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Button(root, text="选择", command=self._choose_output).grid(row=2, column=2, padx=(8, 0), pady=6)

        browser_frame = ttk.LabelFrame(root, text="浏览器", padding=12)
        browser_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 8))
        browser_frame.columnconfigure(1, weight=1)
        ttk.Label(browser_frame, text="Microsoft Edge（日常 Profile）", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6)
        )
        ttk.Label(browser_frame, text="Microsoft Edge：").grid(row=1, column=0, sticky="w", pady=3)
        self.edge_status = tk.Label(browser_frame, text="○ 检测中", fg=STATUS_COLORS["gray"], anchor="w")
        self.edge_status.grid(row=1, column=1, columnspan=2, sticky="ew", pady=3)

        ttk.Label(browser_frame, text="Remote debugging：").grid(row=2, column=0, sticky="w", pady=3)
        self.remote_debugging_status = tk.Label(
            browser_frame, text="○ 检测中", fg=STATUS_COLORS["gray"], anchor="w"
        )
        self.remote_debugging_status.grid(row=2, column=1, columnspan=2, sticky="ew", pady=3)

        ttk.Label(browser_frame, text="授权：").grid(row=3, column=0, sticky="w", pady=3)
        self.authorization_status = tk.Label(
            browser_frame, text="○ 尚未检测", fg=STATUS_COLORS["gray"], anchor="w"
        )
        self.authorization_status.grid(row=3, column=1, columnspan=2, sticky="ew", pady=3)

        ttk.Label(browser_frame, text="Browser sidecar：").grid(row=4, column=0, sticky="w", pady=3)
        self.connection_status = tk.Label(
            browser_frame, text="○ 尚未连接", fg=STATUS_COLORS["gray"], anchor="w"
        )
        self.connection_status.grid(row=4, column=1, columnspan=2, sticky="ew", pady=3)

        ttk.Label(browser_frame, text="页面：").grid(row=5, column=0, sticky="nw", pady=3)
        self.page_count_label = ttk.Label(browser_frame, text="0")
        self.page_count_label.grid(row=5, column=1, sticky="nw", pady=3)
        self.page_urls_label = ttk.Label(browser_frame, text="", wraplength=640, justify="left")
        self.page_urls_label.grid(row=6, column=1, columnspan=2, sticky="ew", pady=(0, 3))

        ttk.Label(browser_frame, text="User Data：").grid(row=7, column=0, sticky="w", pady=3)
        self.user_data_label = ttk.Label(browser_frame, text=str(normal_edge_user_data_dir()))
        self.user_data_label.grid(row=7, column=1, columnspan=2, sticky="w", pady=3)

        browser_buttons = ttk.Frame(browser_frame)
        browser_buttons.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(8, 2))
        self.reconnect_button = ttk.Button(browser_buttons, text="重新连接", command=self._initialize_browser)
        self.reconnect_button.pack(side="left")
        self.select_edge_button = ttk.Button(browser_buttons, text="选择 Edge…", command=self._select_edge)
        self.select_edge_button.pack(side="left", padx=8)
        self.inspect_edge_button = ttk.Button(
            browser_buttons,
            text="打开 edge://inspect",
            command=self._open_edge_inspect,
        )
        self.inspect_edge_button.pack(side="left")

        self.browser_guidance = ttk.Label(
            browser_frame,
            text="正在检测正常 Microsoft Edge……",
            wraplength=790,
            justify="left",
        )
        self.browser_guidance.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        options = ttk.LabelFrame(root, text="导出选项", padding=12)
        options.grid(row=4, column=0, columnspan=3, sticky="ew", pady=8)
        for column in range(3):
            options.columnconfigure(column, weight=1)
        self.download_post = tk.BooleanVar(value=True)
        self.download_comments = tk.BooleanVar(value=True)
        self.markdown = tk.BooleanVar(value=True)
        self.html = tk.BooleanVar(value=True)
        self.json_output = tk.BooleanVar(value=True)
        self.debug = tk.BooleanVar(value=False)
        choices = [
            ("下载帖子图片到本地", self.download_post),
            ("下载评论图片到本地", self.download_comments),
            ("导出 Markdown", self.markdown),
            ("导出 HTML", self.html),
            ("导出 JSON", self.json_output),
            ("Comment Diagnostics（评论诊断）", self.debug),
        ]
        for index, (label, variable) in enumerate(choices):
            ttk.Checkbutton(options, text=label, variable=variable).grid(
                row=index // 3, column=index % 3, sticky="w", padx=5, pady=4
            )

        button_row = ttk.Frame(root)
        button_row.grid(row=5, column=0, columnspan=3, sticky="ew", pady=10)
        self.start_button = ttk.Button(
            button_row,
            text="开始导出",
            command=self._start,
            state="disabled",
        )
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(button_row, text="停止", command=self._stop)
        self.open_button = ttk.Button(button_row, text="打开导出目录", command=self._open_output, state="disabled")
        self.open_button.pack(side="left", padx=8)
        self.captcha_done_button = ttk.Button(
            button_row,
            text="我已完成验证",
            command=self._captcha_completed,
        )
        self.retry_button = ttk.Button(button_row, text="重新尝试", command=self._retry_after_limit)
        self.progress = ttk.Progressbar(button_row, mode="indeterminate")
        self.progress.pack(side="right", fill="x", expand=True, padx=(20, 0))

        ttk.Label(root, text="实时日志：").grid(row=6, column=0, columnspan=3, sticky="w", pady=(5, 4))
        self.log = ScrolledText(root, height=15, wrap="word", font=("Consolas", 10), state="disabled")
        self.log.grid(row=7, column=0, columnspan=3, sticky="nsew")

    def _set_connection_status(self, color: str, text: str) -> None:
        self.connection_status.configure(
            text=f"{'●' if color == 'green' else '○'} {text}", fg=STATUS_COLORS[color]
        )

    @staticmethod
    def _set_indicator(widget: tk.Label, color: str, text: str, *, filled: bool = True) -> None:
        widget.configure(text=f"{'●' if filled else '○'} {text}", fg=STATUS_COLORS[color])

    def _hide_restart_actions(self) -> None:
        return

    def _show_restart_actions(self) -> None:
        self.browser_guidance.configure(
            text="Microsoft Edge 当前实例尚未开启远程调试。\n\n"
            "请打开：\nedge://inspect\n\n"
            "启用：\nAllow remote debugging for this browser instance"
        )

    def _initialize_browser(self, force_open: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.browser_worker and self.browser_worker.is_alive():
            return
        self.edge_ready = False
        self._hide_restart_actions()
        self.reconnect_button.configure(state="disabled", text="正在连接……")
        self.select_edge_button.configure(state="disabled")
        self.inspect_edge_button.configure(state="disabled")
        self._set_connection_status("yellow", "正在启动")
        self._set_indicator(self.authorization_status, "gray", "尚未检测", filled=False)
        self.page_count_label.configure(text="0")
        self.page_urls_label.configure(text="")
        self.browser_guidance.configure(text="正在检查日常 Microsoft Edge 与 chrome-devtools-mcp sidecar……")
        self.browser_worker = threading.Thread(
            target=self._run_browser_init,
            daemon=True,
        )
        self.browser_worker.start()

    def _run_browser_init(self) -> None:
        try:
            environment = inspect_edge_environment()
            self.logger.info("User Data dir: %s", environment.user_data_dir)
            self.logger.info(
                "DevToolsActivePort detected: %s",
                "yes" if environment.devtools_active_port_detected else "no",
            )
            self.events.put(("browser_environment", environment))
            if not environment.running:
                raise RuntimeError("Microsoft Edge 尚未运行。请先启动你的日常 Edge。")
            if not environment.devtools_active_port_detected:
                self.events.put(("remote_debugging_required", None))
                return
            controller = self.browser_controller
            if controller is None:
                controller = ChromeDevToolsMcpController(
                    user_data_dir=environment.user_data_dir,
                    working_dir=self.base_dir,
                    logger=self.logger,
                )
                self.browser_controller = controller
            controller.connect()
            self.events.put(
                (
                    "sidecar_started",
                    {
                        "pid": controller.process_id,
                        "mcp_version": controller.mcp_version,
                        "node_version": controller.node_version,
                    },
                )
            )
            pages = controller.list_pages(
                on_authorization_waiting=lambda: self.events.put(("authorization_waiting", None))
            )
            payload = {
                "edge_executable": self.edge_executable,
                "user_data_dir": environment.user_data_dir,
                "pages": pages,
                "mcp_version": controller.mcp_version,
                "node_version": controller.node_version,
                "pid": controller.process_id,
            }
            self.events.put(("browser_status", payload))
        except Exception as error:
            controller = self.browser_controller
            if controller is not None and not controller.is_available:
                controller.close()
                self.browser_controller = None
            self.events.put(("browser_error", {"error": error}))

    def _apply_browser_status(self, payload: dict[str, object]) -> None:
        self.edge_ready = True
        self.user_data_label.configure(text=str(payload["user_data_dir"]))
        pages = payload.get("pages") or []
        self.page_count_label.configure(text=str(len(pages)))
        self.page_urls_label.configure(text="")
        self._save_settings()
        self._hide_restart_actions()
        self.reconnect_button.configure(state="normal", text="重新连接")
        self.select_edge_button.configure(state="normal")
        self.inspect_edge_button.configure(state="normal")
        self._set_indicator(self.edge_status, "green", "已运行")
        self._set_indicator(self.remote_debugging_status, "green", "已开启")
        self._set_indicator(self.authorization_status, "green", "已允许")
        self._set_connection_status("green", "已连接")
        self.browser_guidance.configure(
            text=f"已连接 Microsoft Edge，可用页面：{len(pages)}。"
        )
        self._update_start_state()

    def _open_edge_inspect(self) -> None:
        """Open Edge's manual authorization page; never click its controls automatically."""

        executable = self.edge_executable or find_edge_executable()
        if executable is None:
            messagebox.showerror("找不到 Edge", "请先选择有效的 msedge.exe。", parent=self)
            return
        try:
            subprocess.Popen((str(executable), "edge://inspect/#remote-debugging"), close_fds=True)
        except OSError as error:
            messagebox.showerror("无法打开 Edge", str(error), parent=self)
            return
        self.browser_guidance.configure(
            text="Microsoft Edge 当前实例尚未开启远程调试。\n\n"
            "请在刚打开的 edge://inspect 中启用：\n"
            "Allow remote debugging for this browser instance\n\n"
            "完成后点击“重新连接”。"
        )
        self._set_indicator(self.remote_debugging_status, "yellow", "等待手工开启", filled=False)

    def _restart_edge(self) -> None:
        self._open_edge_inspect()

    def _defer_edge_connection(self) -> None:
        self.edge_ready = False
        self._hide_restart_actions()
        self.reconnect_button.configure(state="normal", text="重新连接")
        self.inspect_edge_button.configure(state="normal")
        self._set_connection_status("gray", "暂未连接")
        self.browser_guidance.configure(
            text="已暂不连接。Microsoft Edge 保持原状；保存好网页内容后，可点击“重新连接”。"
        )

    def _select_edge(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 Microsoft Edge",
            filetypes=(("Microsoft Edge", "msedge.exe"), ("可执行文件", "*.exe")),
        )
        if not selected:
            return
        candidate = find_edge_executable(selected)
        if candidate is None:
            messagebox.showerror("选择无效", "请选择有效的 msedge.exe。", parent=self)
            return
        self.edge_executable = candidate
        self._save_settings()
        self._initialize_browser()

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output.get() or str(Path.home()))
        if selected:
            self.output.set(selected)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            parse_post_url(self.url.get().strip())
        except ValueError:
            messagebox.showerror("链接无效", "无法识别小黑盒帖子链接。", parent=self)
            return
        output = Path(self.output.get()).expanduser()
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            messagebox.showerror("输出目录无效", str(error), parent=self)
            return
        if not self.edge_ready or self.browser_controller is None:
            messagebox.showerror("浏览器未连接", "请先连接 Microsoft Edge。", parent=self)
            return
        self.request_control = RequestControl(
            listener=lambda state, message: self.events.put(("request_state", (state, message)))
        )
        options = TaskOptions(
            url=self.url.get().strip(), output_dir=output,
            download_post_images=self.download_post.get(),
            download_comment_images=self.download_comments.get(),
            export_markdown=self.markdown.get(), export_html=self.html.get(), export_json=self.json_output.get(),
            debug=self.debug.get(), edge_executable=self.edge_executable,
            request_control=self.request_control, browser_controller=self.browser_controller,
        )
        self.start_button.configure(state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.open_button.configure(state="disabled")
        self.progress.start(12)
        self.worker = threading.Thread(target=self._run, args=(options,), daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        if self.request_control:
            self.request_control.cancel()
            self.stop_button.configure(state="disabled")
            self.browser_guidance.configure(text="正在停止后续浏览器动作……")

    def _update_start_state(self) -> None:
        if not hasattr(self, "start_button"):
            return
        running = bool(self.worker and self.worker.is_alive())
        valid_url = False
        try:
            parse_post_url(self.url.get().strip())
            valid_url = True
        except ValueError:
            pass
        output_text = self.output.get().strip()
        output_valid = bool(output_text)
        self.start_button.configure(state="normal" if self.edge_ready and valid_url and output_valid and not running else "disabled")

    def _run(self, options: TaskOptions) -> None:
        try:
            path = run_export_with_logger(options, self.logger)
            self.events.put(("done", path))
        except Exception as error:
            self.events.put(("error", error))

    def _captcha_completed(self) -> None:
        if self.request_control and self.request_control.submit_captcha_completed():
            self.captcha_done_button.configure(state="disabled")
            self.progress.start(12)

    def _retry_after_limit(self) -> None:
        if self.request_control and self.request_control.submit_retry():
            self.retry_button.configure(state="disabled")
            self.progress.start(12)

    def _hide_request_action_buttons(self) -> None:
        self.captcha_done_button.pack_forget()
        self.retry_button.pack_forget()

    def _restore_idle_controls(self) -> None:
        self._hide_request_action_buttons()
        self.reconnect_button.configure(state="normal", text="重新连接")
        self.select_edge_button.configure(state="normal")
        self.inspect_edge_button.configure(state="normal")
        self.stop_button.pack_forget()
        self.stop_button.configure(state="normal")
        self._update_start_state()

    def _apply_request_state(self, state: RequestState, message: str) -> None:
        self._hide_request_action_buttons()
        if state == RequestState.CAPTCHA_REQUIRED:
            self.progress.stop()
            self.logger.warning("CAPTCHA_REQUIRED")
            self.browser_guidance.configure(
                text="检测到小黑盒验证码。程序已完全停止所有自动请求，不会轮询、刷新或检测登录状态。"
                "请在正常 Edge 的 Heybox Exporter 工作标签中人工完成验证，然后点击“我已完成验证”；"
                "程序只会执行一次轻量检查。"
            )
            self.captcha_done_button.configure(state="normal")
            self.captcha_done_button.pack(side="left", padx=8)
        elif state == RequestState.RATE_LIMITED:
            self.progress.stop()
            self.logger.warning("RATE_LIMITED")
            self.browser_guidance.configure(
                text="小黑盒当前已触发频率限制。\n\n"
                "程序已完全停止所有自动操作，不会定时重试或刷新。\n\n"
                "请等待一段时间后点击“重新尝试”，程序只检查一次当前页面。"
            )
            self.retry_button.configure(state="normal")
            self.retry_button.pack(side="left", padx=8)
        else:
            if self.worker and self.worker.is_alive():
                self.progress.start(12)
            self.browser_guidance.configure(text="单次检查通过，已恢复低频串行抓取。")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", str(payload) + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "browser_status":
                    self._apply_browser_status(payload)  # type: ignore[arg-type]
                elif kind == "browser_environment":
                    environment = payload
                    self.user_data_label.configure(text=str(environment.user_data_dir))
                    self._set_indicator(
                        self.edge_status,
                        "green" if environment.running else "red",
                        "已运行" if environment.running else "未运行",
                        filled=bool(environment.running),
                    )
                    self._set_indicator(
                        self.remote_debugging_status,
                        "green" if environment.devtools_active_port_detected else "yellow",
                        "已开启" if environment.devtools_active_port_detected else "未开启",
                        filled=bool(environment.devtools_active_port_detected),
                    )
                elif kind == "remote_debugging_required":
                    self.edge_ready = False
                    self.reconnect_button.configure(state="normal", text="重新检测")
                    self.select_edge_button.configure(state="normal")
                    self.inspect_edge_button.configure(state="normal")
                    self._set_connection_status("gray", "尚未启动")
                    self._set_indicator(self.authorization_status, "gray", "尚未请求", filled=False)
                    self._show_restart_actions()
                elif kind == "sidecar_started":
                    details = payload
                    self._set_connection_status("yellow", f"已启动（PID {details['pid']}），正在连接")
                    self._set_indicator(self.authorization_status, "yellow", "正在请求", filled=False)
                elif kind == "authorization_waiting":
                    self.logger.info("Authorization waiting")
                    self._set_connection_status("yellow", "已启动，等待授权")
                    self._set_indicator(self.authorization_status, "yellow", "等待 Edge 授权", filled=False)
                    self.inspect_edge_button.configure(state="normal")
                    self.browser_guidance.configure(
                        text="Edge 正在等待远程调试授权。\n\n请在 Edge 中点击“允许”。\n\n"
                        "程序会继续等待当前 sidecar，不会自动点击，也不会重启连接流程。"
                    )
                elif kind == "browser_lifecycle":
                    status, guidance = payload  # type: ignore[misc]
                    self._set_connection_status("yellow", str(status))
                    self.browser_guidance.configure(text=str(guidance))
                elif kind == "browser_error":
                    error_payload = payload  # type: ignore[assignment]
                    error = error_payload["error"]
                    self.edge_ready = False
                    self._hide_restart_actions()
                    self.reconnect_button.configure(state="normal", text="重新连接")
                    self.select_edge_button.configure(state="normal")
                    self.inspect_edge_button.configure(state="normal")
                    self._set_connection_status("red", "连接失败")
                    self._set_indicator(self.authorization_status, "gray", "尚未允许", filled=False)
                    self.browser_guidance.configure(text=str(error))
                elif kind == "request_state":
                    state, message = payload  # type: ignore[misc]
                    self._apply_request_state(state, str(message))
                elif kind == "done":
                    self.progress.stop()
                    self.worker = None
                    self.open_button.configure(state="normal")
                    self._restore_idle_controls()
                    self.request_control = None
                    self.last_output = Path(payload)  # type: ignore[arg-type]
                    messagebox.showinfo("导出完成", f"文件已保存到：\n{payload}", parent=self)
                elif kind == "error":
                    self.progress.stop()
                    self.worker = None
                    self._restore_idle_controls()
                    self.request_control = None
                    messagebox.showerror("导出失败", str(payload), parent=self)
        except queue.Empty:
            pass
        if not self.closing:
            self.after(100, self._drain_events)

    def _open_output(self) -> None:
        path = getattr(self, "last_output", Path(self.output.get()))
        if path.exists():
            os.startfile(path)  # type: ignore[attr-defined]

    def _on_close(self) -> None:
        self.closing = True
        self._save_settings()
        if self.request_control:
            self.request_control.cancel()
        if self.browser_controller is not None:
            self.browser_controller.close()
            self.browser_controller = None
        self.destroy()


def main() -> None:
    app = ExporterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
