from __future__ import annotations

import logging
import json
from dataclasses import dataclass
from pathlib import Path

from ..assets import AssetArchiver
from ..mhtml import MhtmlDocument
from ..models import ExportData
from ..diagnostics import write_comment_diagnostics
from ..utils import safe_filename, unique_directory
from .html_exporter import export_html
from .json_exporter import export_json
from .markdown import export_markdown


@dataclass
class ExportOptions:
    output_parent: Path
    download_post_images: bool = True
    download_comment_images: bool = True
    markdown: bool = True
    html: bool = True
    json: bool = True
    diagnostics: bool = False


def export_all(
    data: ExportData,
    options: ExportOptions,
    logger: logging.Logger,
    mhtml: MhtmlDocument | None = None,
) -> Path:
    options.output_parent.mkdir(parents=True, exist_ok=True)
    output_dir = unique_directory(options.output_parent, data.post.title)
    base_name = safe_filename(data.post.title)
    raw_payloads = data.source.pop("raw_payloads", [])
    AssetArchiver(output_dir, logger, mhtml).archive(
        data,
        post_images=options.download_post_images,
        comment_images=options.download_comment_images,
    )
    if options.markdown:
        logger.info("正在生成 Markdown……")
        export_markdown(data, output_dir / f"{base_name}.md")
    if options.html:
        logger.info("正在生成 HTML……")
        export_html(data, output_dir / f"{base_name}.html")
    if options.json:
        logger.info("正在生成 JSON……")
        export_json(data, output_dir / f"{base_name}.json")
    if options.diagnostics:
        debug_dir = output_dir / "debug"
        write_comment_diagnostics(data, debug_dir)
        if isinstance(raw_payloads, list) and raw_payloads:
            raw_dir = debug_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            for index, item in enumerate(raw_payloads, start=1):
                (raw_dir / f"{index:04d}.json").write_text(
                    json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
                )
    logger.info("导出完成：%s", output_dir.resolve())
    return output_dir
