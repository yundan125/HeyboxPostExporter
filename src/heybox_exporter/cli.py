from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .browser_connection import BrowserMode
from .service import TaskOptions, run_export


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="完整导出小黑盒帖子、一级评论和楼中楼回复")
    parser.add_argument("url", nargs="?", help="小黑盒帖子链接")
    parser.add_argument("--mhtml", type=Path, help="离线解析 MHTML 样本，不访问网站")
    parser.add_argument("-o", "--output", type=Path, default=Path("exports"), help="导出父目录")
    parser.add_argument("--edge-executable", type=Path, help="手动指定系统 msedge.exe")
    parser.add_argument("--debug", action="store_true", help="保存接口原始 JSON 和最终 DOM")
    parser.add_argument("--no-post-images", action="store_true", help="不下载帖子图片")
    parser.add_argument("--no-comment-images", action="store_true", help="不下载评论图片")
    parser.add_argument("--no-markdown", action="store_true", help="不生成 Markdown")
    parser.add_argument("--no-html", action="store_true", help="不生成 HTML")
    parser.add_argument("--no-json", action="store_true", help="不生成 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.url and not args.mhtml:
        build_parser().error("请提供帖子链接，或使用 --mhtml 指定离线样本")
    options = TaskOptions(
        url=args.url or "",
        mhtml_path=args.mhtml,
        output_dir=args.output,
        download_post_images=not args.no_post_images,
        download_comment_images=not args.no_comment_images,
        export_markdown=not args.no_markdown,
        export_html=not args.no_html,
        export_json=not args.no_json,
        browser_mode=BrowserMode.EDGE,
        show_browser=True,
        debug=args.debug,
        edge_executable=args.edge_executable,
    )
    try:
        path = run_export(options, print)
        print(f"\n导出完成：{path}")
        return 0
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"\n导出失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
