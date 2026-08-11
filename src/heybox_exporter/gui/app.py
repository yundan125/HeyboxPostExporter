from __future__ import annotations

import json
import os
import queue
import re
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
ACCENT = "#0f6cbd"
ACCENT_ACTIVE = "#115ea3"


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
        self.geometry("960x740")
        self.minsize(820, 680)
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
        self.browser_details_visible = False
        self.records_visible = False
        self.connection_page_count = 0
        self.connection_component_status = "正在连接"
        self.user_records: list[str] = []
        self.last_output = Path(self.settings.get("last_output", "")) if self.settings.get("last_output") else None
        self.progress_counts = {"comments": 0, "replies": 0, "images": 0, "image_failures": 0}
        self.current_formats: list[str] = []
        self.logger = create_logger(self.base_dir, lambda text: self.events.put(("log", text)))
        self._configure_styles()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_events)
        self.after(350, self._initialize_browser)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        if sys.platform == "win32" and "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TFrame", background="#f7f7f7")
        style.configure("TLabel", background="#f7f7f7", foreground="#242424", font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"), foreground="#242424")
        style.configure("Hint.TLabel", font=("Microsoft YaHei UI", 8), foreground="#667085")
        style.configure("TCheckbutton", background="#f7f7f7", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(12, 5))
        style.configure("TEntry", padding=(7, 6), font=("Microsoft YaHei UI", 9))
        style.configure("Horizontal.TProgressbar", thickness=6)

    def _load_settings(self) -> dict[str, object]:
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_settings(self) -> None:
        payload = {
            "edge_executable": str(self.edge_executable) if self.edge_executable else "",
            "last_output": str(self.last_output) if self.last_output else "",
        }
        try:
            self.settings_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            self.logger.warning("无法保存设置：%s", error)

    @staticmethod
    def _separator(parent: tk.Misc) -> ttk.Separator:
        return ttk.Separator(parent, orient="horizontal")

    def _build(self) -> None:
        self.configure(background="#f7f7f7")
        root = ttk.Frame(self, padding=(26, 20, 26, 18))
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(11, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="小黑盒帖子完整导出", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        ttk.Label(
            header,
            text="完整保存帖子正文、评论、回复和图片",
            style="Hint.TLabel",
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(3, 0))

        fields = ttk.Frame(root)
        fields.grid(row=1, column=0, sticky="ew")
        fields.columnconfigure(0, weight=1)
        ttk.Label(fields, text="帖子链接", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.url = tk.StringVar()
        self.url.trace_add("write", lambda *_: self._on_url_changed())
        url_box = ttk.Frame(fields)
        url_box.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.url_entry = ttk.Entry(url_box, textvariable=self.url)
        self.url_entry.pack(fill="x")
        self.url_placeholder = ttk.Label(url_box, text="粘贴小黑盒帖子链接", style="Hint.TLabel", cursor="xterm")
        self.url_placeholder.place(x=10, rely=0.5, anchor="w")
        self.url_placeholder.bind("<Button-1>", lambda _event: self.url_entry.focus_set())
        self.url_feedback = tk.Label(
            fields,
            text=" ",
            bg="#f7f7f7",
            fg=STATUS_COLORS["gray"],
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        )
        self.url_feedback.grid(row=2, column=0, sticky="ew", pady=(2, 3))

        ttk.Label(fields, text="保存到", style="Section.TLabel").grid(row=3, column=0, sticky="w")
        output_row = ttk.Frame(fields)
        output_row.grid(row=4, column=0, sticky="ew", pady=(5, 0))
        output_row.columnconfigure(0, weight=1)
        self.output = tk.StringVar(value=str((Path.home() / "Documents" / "小黑盒帖子导出").resolve()))
        self.output.trace_add("write", lambda *_: self._on_output_changed())
        ttk.Entry(output_row, textvariable=self.output).grid(row=0, column=0, sticky="ew")
        ttk.Button(output_row, text="选择", command=self._choose_output).grid(row=0, column=1, padx=(8, 0))
        self.open_folder_button = ttk.Button(output_row, text="打开文件夹", command=self._open_selected_output)
        self.open_folder_button.grid(row=0, column=2, padx=(8, 0))

        self._separator(root).grid(row=2, column=0, sticky="ew", pady=9)

        browser_frame = ttk.Frame(root)
        browser_frame.grid(row=3, column=0, sticky="ew")
        browser_frame.columnconfigure(0, weight=1)
        ttk.Label(browser_frame, text="浏览器", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        browser_actions = ttk.Frame(browser_frame)
        browser_actions.grid(row=0, column=1, rowspan=2, sticky="e")
        self.inspect_edge_button = ttk.Button(
            browser_actions, text="打开连接设置", command=self._open_edge_inspect
        )
        self.select_edge_button = ttk.Button(browser_actions, text="选择浏览器", command=self._select_edge)
        self.reconnect_button = ttk.Button(browser_actions, text="重新连接", command=self._initialize_browser)
        self.reconnect_button.pack(side="right")
        self.browser_details_button = ttk.Button(
            browser_actions, text="查看连接详情", command=self._toggle_browser_details
        )
        self.browser_details_button.pack(side="right", padx=(0, 8))

        self.browser_status = tk.Label(
            browser_frame,
            text="● 正在连接 Microsoft Edge…",
            bg="#f7f7f7",
            fg=STATUS_COLORS["yellow"],
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="w",
        )
        self.browser_status.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        self.browser_guidance = ttk.Label(browser_frame, text="请稍候", style="Hint.TLabel")
        self.browser_guidance.grid(row=2, column=0, columnspan=2, sticky="w", padx=(16, 0), pady=(2, 0))

        self.browser_details = ttk.Frame(browser_frame)
        self.browser_details_label = ttk.Label(
            self.browser_details,
            text="Microsoft Edge · 当前浏览器配置 · 0 个页面 · 正在连接",
            style="Hint.TLabel",
        )
        self.browser_details_label.pack(anchor="w", padx=(16, 0))

        self._separator(root).grid(row=4, column=0, sticky="ew", pady=9)

        options = ttk.Frame(root)
        options.grid(row=5, column=0, sticky="ew")
        options.columnconfigure(0, weight=1)
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="导出内容", style="Section.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 5)
        )
        self.download_post = tk.BooleanVar(value=True)
        self.download_comments = tk.BooleanVar(value=True)
        self.markdown = tk.BooleanVar(value=True)
        self.html = tk.BooleanVar(value=True)
        self.json_output = tk.BooleanVar(value=True)
        self.debug = tk.BooleanVar(value=False)
        choices = [
            ("HTML 网页", self.html),
            ("Markdown", self.markdown),
            ("JSON 数据", self.json_output),
            ("帖子图片", self.download_post),
            ("评论图片", self.download_comments),
            ("评论诊断", self.debug),
        ]
        for index, (label, variable) in enumerate(choices):
            ttk.Checkbutton(options, text=label, variable=variable).grid(
                row=1 + index // 2, column=index % 2, sticky="w", padx=(0, 40), pady=3
            )
        ttk.Label(options, text="用于排查评论数量不一致问题", style="Hint.TLabel").grid(
            row=4, column=1, sticky="w", padx=(22, 0), pady=(0, 1)
        )

        action_row = ttk.Frame(root)
        action_row.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        action_row.columnconfigure(0, weight=1)
        self.start_button = tk.Button(
            action_row,
            text="开始导出",
            command=self._start,
            state="disabled",
            relief="flat",
            bd=0,
            height=2,
            bg=ACCENT,
            activebackground=ACCENT_ACTIVE,
            fg="white",
            activeforeground="white",
            disabledforeground="#8a8886",
            font=("Microsoft YaHei UI", 10, "bold"),
            cursor="hand2",
        )
        self.start_button.grid(row=0, column=0, sticky="ew")
        self.stop_button = ttk.Button(action_row, text="停止", command=self._stop)
        self.stop_button.grid(row=0, column=1, padx=(8, 0), sticky="ns")
        self.stop_button.grid_remove()
        self.captcha_done_button = ttk.Button(action_row, text="我已完成验证", command=self._captcha_completed)
        self.captcha_done_button.grid(row=0, column=1, padx=(8, 0), sticky="ns")
        self.captcha_done_button.grid_remove()
        self.retry_button = ttk.Button(action_row, text="重新尝试", command=self._retry_after_limit)
        self.retry_button.grid(row=0, column=1, padx=(8, 0), sticky="ns")
        self.retry_button.grid_remove()

        progress_frame = ttk.Frame(root)
        progress_frame.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        progress_frame.columnconfigure(0, weight=1)
        self.progress_status = tk.Label(
            progress_frame,
            text="等待开始导出",
            bg="#f7f7f7",
            fg="#242424",
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        )
        self.progress_status.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100, value=0)
        self.progress.grid(row=1, column=0, sticky="ew")

        self.completion_frame = ttk.Frame(root)
        self.completion_frame.grid(row=8, column=0, sticky="ew", pady=(7, 0))
        self.completion_frame.columnconfigure(0, weight=1)
        self.completion_title = tk.Label(
            self.completion_frame,
            text="✓ 导出完成",
            bg="#f7f7f7",
            fg=STATUS_COLORS["green"],
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
        )
        self.completion_title.grid(row=0, column=0, sticky="ew")
        self.completion_summary = ttk.Label(self.completion_frame, text="", style="Hint.TLabel")
        self.completion_summary.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.completion_path = ttk.Label(self.completion_frame, text="", style="Hint.TLabel", wraplength=720)
        self.completion_path.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        self.open_result_button = ttk.Button(self.completion_frame, text="打开导出目录", command=self._open_output)
        self.open_result_button.grid(row=0, column=1, rowspan=3, padx=(10, 0), sticky="e")
        self.completion_frame.grid_remove()

        self._separator(root).grid(row=9, column=0, sticky="ew", pady=(5, 4))

        records_header = ttk.Frame(root)
        records_header.grid(row=10, column=0, sticky="ew")
        records_header.columnconfigure(0, weight=1)
        ttk.Label(records_header, text="运行记录", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.records_toggle = ttk.Button(records_header, text="展开", command=self._toggle_records)
        self.records_toggle.grid(row=0, column=1, sticky="e")
        self.latest_record = ttk.Label(records_header, text="正在连接 Microsoft Edge…", style="Hint.TLabel")
        self.latest_record.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        self.records_frame = ttk.Frame(root)
        self.records_frame.grid(row=11, column=0, sticky="nsew", pady=(7, 0))
        self.records_frame.rowconfigure(0, weight=1)
        self.records_frame.columnconfigure(0, weight=1)
        self.log = ScrolledText(
            self.records_frame,
            height=6,
            wrap="word",
            font=("Microsoft YaHei UI", 9),
            state="disabled",
            relief="solid",
            bd=1,
            background="#ffffff",
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        self.records_frame.grid_remove()
        self._on_output_changed()

    def _set_browser_status(self, color: str, title: str, guidance: str) -> None:
        self.browser_status.configure(text=f"● {title}", fg=STATUS_COLORS[color])
        self.browser_guidance.configure(text=guidance)

    def _toggle_browser_details(self) -> None:
        self.browser_details_visible = not self.browser_details_visible
        if self.browser_details_visible:
            self.browser_details.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
            self.browser_details_button.configure(text="收起连接详情")
            if self.winfo_height() < 760:
                self.geometry(f"{max(self.winfo_width(), 960)}x760")
        else:
            self.browser_details.grid_remove()
            self.browser_details_button.configure(text="查看连接详情")

    def _refresh_browser_details(self) -> None:
        self.browser_details_label.configure(
            text=(
                "Microsoft Edge · 当前浏览器配置 · "
                f"{self.connection_page_count} 个页面 · {self.connection_component_status}"
            )
        )

    def _toggle_records(self) -> None:
        self.records_visible = not self.records_visible
        if self.records_visible:
            self.records_frame.grid()
            self.records_toggle.configure(text="收起")
        else:
            self.records_frame.grid_remove()
            self.records_toggle.configure(text="展开")

    def _append_record(self, text: str) -> None:
        text = text.strip()
        if not text or (self.user_records and self.user_records[-1] == text):
            return
        self.user_records.append(text)
        self.user_records = self.user_records[-200:]
        self.latest_record.configure(text=text)
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_url_changed(self) -> None:
        value = self.url.get().strip()
        self.url_placeholder.place_forget() if value else self.url_placeholder.place(x=10, rely=0.5, anchor="w")
        if not value:
            self.url_feedback.configure(text=" ", fg=STATUS_COLORS["gray"])
        else:
            try:
                parsed = parse_post_url(value)
            except ValueError:
                self.url_feedback.configure(text="无法识别这个小黑盒帖子链接", fg=STATUS_COLORS["red"])
            else:
                self.url_feedback.configure(text=f"✓ 已识别帖子 ID：{parsed.link_id}", fg=STATUS_COLORS["green"])
        self._update_start_state()

    def _on_output_changed(self) -> None:
        if not hasattr(self, "open_folder_button"):
            return
        try:
            valid = Path(self.output.get()).expanduser().is_dir()
        except (OSError, ValueError):
            valid = False
        self.open_folder_button.configure(state="normal" if valid else "disabled")
        self._update_start_state()

    def _show_restart_actions(self) -> None:
        self._set_browser_status(
            "yellow",
            "需要开启浏览器连接",
            "请打开 Edge 设置页面并允许本工具连接",
        )
        if not self.inspect_edge_button.winfo_manager():
            self.inspect_edge_button.pack(side="right", padx=(0, 8))

    def _hide_secondary_browser_actions(self) -> None:
        self.inspect_edge_button.pack_forget()
        self.select_edge_button.pack_forget()

    def _initialize_browser(self, force_open: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.browser_worker and self.browser_worker.is_alive():
            return
        self.edge_ready = False
        self._hide_secondary_browser_actions()
        self.reconnect_button.configure(state="disabled", text="正在连接…")
        self.connection_component_status = "正在连接"
        self._refresh_browser_details()
        self._set_browser_status("yellow", "正在连接 Microsoft Edge…", "请稍候")
        self._update_start_state()
        self.browser_worker = threading.Thread(target=self._run_browser_init, daemon=True)
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
                raise RuntimeError("Microsoft Edge is not running")
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
            self.events.put(("sidecar_started", {"pid": controller.process_id}))
            pages = controller.list_pages(
                on_authorization_waiting=lambda: self.events.put(("authorization_waiting", None))
            )
            self.events.put(("browser_status", {"pages": pages}))
        except Exception as error:
            controller = self.browser_controller
            if controller is not None and not controller.is_available:
                controller.close()
                self.browser_controller = None
            self.events.put(("browser_error", {"error": error}))

    def _apply_browser_status(self, payload: dict[str, object]) -> None:
        self.edge_ready = True
        pages = payload.get("pages") or []
        self.connection_page_count = len(pages)  # type: ignore[arg-type]
        self.connection_component_status = "已连接"
        self._refresh_browser_details()
        self._save_settings()
        self._hide_secondary_browser_actions()
        self.reconnect_button.configure(state="normal", text="重新连接")
        self._set_browser_status("green", "Microsoft Edge 已连接", "已准备好，可以开始导出")
        self._append_record("已连接 Microsoft Edge")
        self._update_start_state()

    def _open_edge_inspect(self) -> None:
        executable = self.edge_executable or find_edge_executable()
        if executable is None:
            messagebox.showerror("找不到浏览器", "请先选择 Microsoft Edge。", parent=self)
            if not self.select_edge_button.winfo_manager():
                self.select_edge_button.pack(side="right", padx=(0, 8))
            return
        try:
            subprocess.Popen((str(executable), "edge://inspect/#remote-debugging"), close_fds=True)
        except OSError:
            messagebox.showerror("无法打开浏览器", "请确认 Microsoft Edge 可以正常启动。", parent=self)
            return
        self._set_browser_status(
            "yellow",
            "等待浏览器授权",
            "请在 Microsoft Edge 弹出的窗口中点击“允许”，完成后重新连接",
        )

    def _restart_edge(self) -> None:
        self._open_edge_inspect()

    def _defer_edge_connection(self) -> None:
        self.edge_ready = False
        self._hide_secondary_browser_actions()
        self.reconnect_button.configure(state="normal", text="重新连接")
        self._set_browser_status("gray", "Microsoft Edge 尚未连接", "准备好浏览器后重新连接")
        self._update_start_state()

    def _select_edge(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 Microsoft Edge",
            filetypes=(("Microsoft Edge", "msedge.exe"), ("可执行文件", "*.exe")),
        )
        if not selected:
            return
        candidate = find_edge_executable(selected)
        if candidate is None:
            messagebox.showerror("选择无效", "请选择 Microsoft Edge 程序。", parent=self)
            return
        self.edge_executable = candidate
        self._save_settings()
        self._initialize_browser()

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output.get() or str(Path.home()), title="选择保存位置")
        if selected:
            self.output.set(selected)

    def _open_selected_output(self) -> None:
        try:
            path = Path(self.output.get()).expanduser()
        except (OSError, ValueError):
            return
        if path.is_dir():
            os.startfile(path)  # type: ignore[attr-defined]

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            parse_post_url(self.url.get().strip())
        except ValueError:
            messagebox.showerror("链接无效", "无法识别这个小黑盒帖子链接。", parent=self)
            return
        output = Path(self.output.get()).expanduser()
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError:
            messagebox.showerror("保存位置不可用", "无法创建这个文件夹，请选择其他位置。", parent=self)
            return
        if not self.edge_ready or self.browser_controller is None:
            messagebox.showerror("浏览器未连接", "请先连接 Microsoft Edge。", parent=self)
            return
        self.request_control = RequestControl(
            listener=lambda state, message: self.events.put(("request_state", (state, message)))
        )
        options = TaskOptions(
            url=self.url.get().strip(),
            output_dir=output,
            download_post_images=self.download_post.get(),
            download_comment_images=self.download_comments.get(),
            export_markdown=self.markdown.get(),
            export_html=self.html.get(),
            export_json=self.json_output.get(),
            debug=self.debug.get(),
            edge_executable=self.edge_executable,
            request_control=self.request_control,
            browser_controller=self.browser_controller,
        )
        self.progress_counts = {"comments": 0, "replies": 0, "images": 0, "image_failures": 0}
        self.current_formats = [
            name
            for name, enabled in (("HTML", self.html.get()), ("Markdown", self.markdown.get()), ("JSON", self.json_output.get()))
            if enabled
        ]
        self.completion_frame.grid_remove()
        self.latest_record.grid()
        self.start_button.configure(state="disabled", text="正在导出…")
        self.stop_button.grid()
        self.progress_status.configure(text="正在读取帖子内容…", fg="#242424")
        self._append_record("正在读取帖子内容")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self.worker = threading.Thread(target=self._run, args=(options,), daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        if self.request_control:
            self.request_control.cancel()
            self.stop_button.configure(state="disabled")
            self.progress_status.configure(text="正在停止…", fg=STATUS_COLORS["yellow"])
            self._append_record("正在停止导出")

    def _set_start_enabled(self, enabled: bool) -> None:
        self.start_button.configure(
            state="normal" if enabled else "disabled",
            bg=ACCENT if enabled else "#e1dfdd",
            activebackground=ACCENT_ACTIVE if enabled else "#e1dfdd",
            fg="white" if enabled else "#8a8886",
            cursor="hand2" if enabled else "arrow",
        )

    def _update_start_state(self) -> None:
        if not hasattr(self, "start_button"):
            return
        running = bool(self.worker and self.worker.is_alive())
        try:
            parse_post_url(self.url.get().strip())
            valid_url = True
        except ValueError:
            valid_url = False
        output_valid = bool(self.output.get().strip())
        self._set_start_enabled(self.edge_ready and valid_url and output_valid and not running)

    def _run(self, options: TaskOptions) -> None:
        try:
            path = run_export_with_logger(options, self.logger)
            self.events.put(("done", path))
        except Exception as error:
            self.events.put(("error", error))

    def _captcha_completed(self) -> None:
        if self.request_control and self.request_control.submit_captcha_completed():
            self.captcha_done_button.configure(state="disabled")
            self.progress_status.configure(text="正在检查验证结果…", fg="#242424")
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(12)

    def _retry_after_limit(self) -> None:
        if self.request_control and self.request_control.submit_retry():
            self.retry_button.configure(state="disabled")
            self.progress_status.configure(text="正在重新尝试…", fg="#242424")
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(12)

    def _hide_request_action_buttons(self) -> None:
        self.captcha_done_button.grid_remove()
        self.retry_button.grid_remove()

    def _restore_idle_controls(self) -> None:
        self._hide_request_action_buttons()
        self.reconnect_button.configure(state="normal", text="重新连接")
        self.stop_button.grid_remove()
        self.stop_button.configure(state="normal")
        self._update_start_state()

    def _apply_request_state(self, state: RequestState, message: str) -> None:
        self._hide_request_action_buttons()
        if state == RequestState.CAPTCHA_REQUIRED:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.logger.warning("CAPTCHA_REQUIRED")
            self.progress_status.configure(
                text="需要完成安全验证 · 小黑盒要求进行安全验证，请在浏览器中完成验证",
                fg=STATUS_COLORS["yellow"],
            )
            self._append_record("小黑盒要求安全验证")
            self.captcha_done_button.configure(state="normal")
            self.captcha_done_button.grid()
        elif state == RequestState.RATE_LIMITED:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.logger.warning("RATE_LIMITED")
            self.progress_status.configure(
                text="操作过于频繁 · 小黑盒暂时限制了继续加载，请稍后再试",
                fg=STATUS_COLORS["red"],
            )
            self._append_record("评论加载暂时受限")
            self.retry_button.configure(state="normal")
            self.retry_button.grid()
        else:
            if self.worker and self.worker.is_alive():
                self.progress.configure(mode="indeterminate", value=0)
                self.progress.start(12)
            self.progress_status.configure(text="正在继续导出…", fg="#242424")
            self._append_record("已继续导出")

    def _handle_log_message(self, message: str) -> None:
        hidden = re.compile(
            r"MCP|sidecar|Puppeteer|CDP|DevTools|User Data|PID|Node version|list_pages|request[_ ]?id|reqid|JSON-RPC|tool_name|stdio|Network\.",
            re.IGNORECASE,
        )
        if hidden.search(message):
            return
        match = re.search(r"正在加载评论：一级评论\s*(\d+)，回复\s*(\d+)", message)
        if match:
            comments, replies = int(match.group(1)), int(match.group(2))
            self.progress_counts["comments"] = comments
            self.progress_counts["replies"] = replies
            text = f"正在加载评论 · {comments} 条评论 / {replies} 条回复"
            self.progress_status.configure(text=text, fg="#242424")
            self._append_record(text)
            return
        match = re.search(r"正在展开：(\d+)\s*/\s*(\d+)", message)
        if match:
            text = f"正在展开更多回复… · {match.group(1)} / {match.group(2)}"
            self.progress_status.configure(text=text, fg="#242424")
            self._append_record(text)
            return
        match = re.search(r"正在下载图片：(\d+)\s*/\s*(\d+)", message)
        if match:
            self.progress_counts["images"] = int(match.group(1))
            text = f"正在保存图片 · {match.group(1)} / {match.group(2)}"
            self.progress_status.configure(text=text, fg="#242424")
            self._append_record(text)
            return
        match = re.search(r"已获取\s*(\d+)\s*条一级评论", message)
        if match:
            self.progress_counts["comments"] = int(match.group(1))
            self._append_record(f"已获取 {match.group(1)} 条评论")
            return
        match = re.search(r"已获取\s*(\d+)\s*条回复", message)
        if match:
            self.progress_counts["replies"] = int(match.group(1))
            self._append_record(f"已获取 {match.group(1)} 条回复")
            return
        if message.startswith("正在读取帖子"):
            self.progress_status.configure(text="正在读取帖子内容…", fg="#242424")
            self._append_record("正在读取帖子内容")
        elif message.startswith("已获取原帖正文") or message.startswith("找到目标帖子"):
            self._append_record("已找到目标帖子")
        elif message.startswith("正在加载评论"):
            self.progress_status.configure(text="正在加载评论…", fg="#242424")
            self._append_record("正在加载评论")
        elif message.startswith("正在生成"):
            formats = "、".join(self.current_formats) if self.current_formats else "导出文件"
            text = f"正在生成 {formats}…"
            self.progress_status.configure(text=text, fg="#242424")
            self._append_record(text)
        elif message.startswith("图片保存失败"):
            self.progress_counts["image_failures"] += 1
            self._append_record(f"有 {self.progress_counts['image_failures']} 张图片保存失败")
        elif message.startswith("浏览器连接中断"):
            self._append_record("浏览器连接已断开，正在重新连接")

    def _read_export_summary(self, path: Path) -> tuple[int, int, int, str]:
        comments = self.progress_counts["comments"]
        replies = self.progress_counts["replies"]
        images = self.progress_counts["images"]
        completeness = "unknown"
        for json_path in path.glob("*.json"):
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            statistics = payload.get("statistics") or {}
            comments = int(statistics.get("primary_comments") or comments)
            replies = int(statistics.get("replies") or replies)
            completeness = str(payload.get("completeness") or statistics.get("completeness") or "unknown")
            media = payload.get("media") or {}
            all_media = list(media.get("post") or []) + list(media.get("comments") or []) + list(media.get("replies") or [])
            if not all_media:
                post = payload.get("post") or {}
                all_media.extend(list(post.get("images") or []))
                for comment in payload.get("comments") or []:
                    if not isinstance(comment, dict):
                        continue
                    all_media.extend(list(comment.get("images") or []))
                    for reply in comment.get("replies") or []:
                        if isinstance(reply, dict):
                            all_media.extend(list(reply.get("images") or []))
            saved_images = sum(1 for item in all_media if isinstance(item, dict) and item.get("local_path"))
            images = saved_images or images
            break
        return comments, replies, images, completeness

    def _show_completion(self, path: Path) -> None:
        if self.winfo_height() < 780:
            self.geometry(f"{max(self.winfo_width(), 960)}x780")
        comments, replies, images, completeness = self._read_export_summary(path)
        self.completion_title.configure(text="✓ 导出完成", fg=STATUS_COLORS["green"])
        summary = f"{comments} 条评论 · {replies} 条回复 · {images} 张图片"
        if self.current_formats:
            summary += f"    已生成：{' · '.join(self.current_formats)}"
        if completeness == "partial":
            self.completion_title.configure(text="⚠ 部分内容可能未完整加载", fg=STATUS_COLORS["yellow"])
            summary += "    部分评论或回复可能没有加载完成，已获取的内容仍然正常保存。"
        elif completeness in {"complete", "complete_visible"}:
            summary += "    ✓ 已保存所有当前可查看内容"
        self.completion_summary.configure(text=summary)
        self.completion_path.configure(text=f"保存位置：{path}")
        self.completion_frame.grid()
        self.latest_record.grid_remove()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._handle_log_message(str(payload))
                elif kind == "browser_status":
                    self._apply_browser_status(payload)  # type: ignore[arg-type]
                elif kind == "browser_environment":
                    environment = payload
                    if environment.running:
                        self._set_browser_status("yellow", "正在连接 Microsoft Edge…", "请稍候")
                    else:
                        self._set_browser_status("red", "无法连接 Microsoft Edge", "请确认 Edge 已打开，然后重新连接")
                elif kind == "remote_debugging_required":
                    self.edge_ready = False
                    self.reconnect_button.configure(state="normal", text="重新连接")
                    self.connection_component_status = "等待设置"
                    self._refresh_browser_details()
                    self._show_restart_actions()
                    self._append_record("需要开启浏览器连接")
                    self._update_start_state()
                elif kind == "sidecar_started":
                    self.connection_component_status = "正在连接"
                    self._refresh_browser_details()
                    self._set_browser_status("yellow", "正在连接 Microsoft Edge…", "请稍候")
                elif kind == "authorization_waiting":
                    self.logger.info("Authorization waiting")
                    self._set_browser_status(
                        "yellow",
                        "等待浏览器授权",
                        "请在 Microsoft Edge 弹出的窗口中点击“允许”",
                    )
                    self._append_record("等待浏览器授权")
                elif kind == "browser_lifecycle":
                    self._set_browser_status("yellow", "正在恢复浏览器连接…", "请稍候")
                elif kind == "browser_error":
                    self.logger.error("Browser connection failed: %s", payload["error"])  # type: ignore[index]
                    self.edge_ready = False
                    self._hide_secondary_browser_actions()
                    self.reconnect_button.configure(state="normal", text="重新连接")
                    self.connection_component_status = "未连接"
                    self._refresh_browser_details()
                    self._set_browser_status(
                        "red",
                        "无法连接 Microsoft Edge",
                        "请确认 Edge 已打开，然后重新连接。详细信息已写入日志文件。",
                    )
                    if self.edge_executable is None:
                        self.select_edge_button.pack(side="right", padx=(0, 8))
                    self._append_record("无法连接 Microsoft Edge")
                    self._update_start_state()
                elif kind == "request_state":
                    state, message = payload  # type: ignore[misc]
                    self._apply_request_state(state, str(message))
                elif kind == "done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=100)
                    self.worker = None
                    self.request_control = None
                    self.last_output = Path(payload)  # type: ignore[arg-type]
                    self._restore_idle_controls()
                    self.start_button.configure(text="再次导出")
                    self.progress_status.configure(text="导出完成", fg=STATUS_COLORS["green"])
                    self._append_record("导出完成")
                    self._show_completion(self.last_output)
                    self._save_settings()
                elif kind == "error":
                    self.logger.error("Export failed: %s", payload)
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.worker = None
                    self.request_control = None
                    self._restore_idle_controls()
                    self.start_button.configure(text="开始导出")
                    self.progress_status.configure(text="导出失败", fg=STATUS_COLORS["red"])
                    self._append_record("导出失败，详细信息已写入日志文件")
                    messagebox.showerror("导出失败", "导出未完成。详细信息已写入 logs/latest.log。", parent=self)
        except queue.Empty:
            pass
        if not self.closing:
            self.after(100, self._drain_events)

    def _open_output(self) -> None:
        path = self.last_output or Path(self.output.get())
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
