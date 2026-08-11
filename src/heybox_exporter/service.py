from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .dom_parser import parse_mhtml
from .browser_controller import ChromeDevToolsMcpController, normal_edge_user_data_dir
from .exporter import ExportOptions, export_all
from .logging_setup import create_logger
from .request_control import RequestControl
from .url_parser import parse_post_url
from .mcp_collector import McpBrowserCollector


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


@dataclass
class TaskOptions:
    url: str = ""
    mhtml_path: Path | None = None
    output_dir: Path = Path("exports")
    download_post_images: bool = True
    download_comment_images: bool = True
    export_markdown: bool = True
    export_html: bool = True
    export_json: bool = True
    browser_mode: str = "edge"
    show_browser: bool = False
    debug: bool = False
    browser_context_index: int | None = None
    edge_executable: Path | None = None
    request_control: RequestControl | None = None
    browser_controller: ChromeDevToolsMcpController | None = None


def run_export(options: TaskOptions, progress: Callable[[str], None] | None = None) -> Path:
    base = application_dir()
    logger = create_logger(base, progress)
    return run_export_with_logger(options, logger)


def run_export_with_logger(options: TaskOptions, logger: logging.Logger) -> Path:
    output = options.output_dir.expanduser().resolve()
    document = None
    if options.mhtml_path:
        logger.info("正在离线分析 MHTML：%s", options.mhtml_path)
        data, document = parse_mhtml(options.mhtml_path)
        if options.debug:
            debug_dir = application_dir() / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "page.html").write_text(document.html, encoding="utf-8")
    else:
        parsed = parse_post_url(options.url)
        controller = options.browser_controller
        owns_controller = controller is None
        if controller is None:
            controller = ChromeDevToolsMcpController(
                user_data_dir=normal_edge_user_data_dir(), working_dir=application_dir(), logger=logger
            )
        try:
            controller.connect()
            document = None
            data = McpBrowserCollector(
                controller, logger, control=options.request_control, debug=options.debug
            ).collect(parsed)
        finally:
            if owns_controller:
                controller.close()
    export_options = ExportOptions(
        output_parent=output,
        download_post_images=options.download_post_images,
        download_comment_images=options.download_comment_images,
        markdown=options.export_markdown,
        html=options.export_html,
        json=options.export_json,
        diagnostics=options.debug,
    )
    return export_all(data, export_options, logger, document)
