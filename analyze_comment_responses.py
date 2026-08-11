from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterator


STATUS_PARTS = (
    "hide", "hidden", "fold", "visible", "visibility", "display", "delete",
    "deleted", "audit", "review", "shield", "ban", "blocked", "status", "state",
    "meaningless",
)


def _comment_id(mapping: dict[str, Any], inherited: str) -> str:
    for key in ("commentid", "comment_id", "id"):
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return inherited


def walk(value: Any, path: str = "$", comment_id: str = "") -> Iterator[tuple[str, str, Any, str]]:
    if isinstance(value, dict):
        current_id = _comment_id(value, comment_id)
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield str(key), child_path, child, current_id
            yield from walk(child, child_path, current_id)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, f"{path}[{index}]", comment_id)


def printable(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = repr(value)
    return text if len(text) <= 240 else text[:237] + "..."


def is_count_field(field: str) -> bool:
    lowered = field.lower()
    if lowered in {"has_more", "has_more_floors", "total", "count", "total_page"}:
        return True
    subject = any(part in lowered for part in ("comment", "reply", "child", "floor"))
    metric = any(part in lowered for part in ("_num", "_count", "total", "has_more"))
    return subject and metric


def analyze(directory: Path) -> str:
    files = sorted(directory.glob("*.json"))
    if not files:
        raise SystemExit(f"没有找到 JSON：{directory.resolve()}")
    count_rows: Counter[tuple[str, str, str, str]] = Counter()
    status_rows: Counter[tuple[str, str, str, str]] = Counter()
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for field, json_path, value, comment_id in walk(payload):
            lowered = field.lower()
            row = (field, f"{path.name}:{json_path}", printable(value), comment_id)
            if is_count_field(lowered):
                count_rows[row] += 1
            if any(part in lowered for part in STATUS_PARTS):
                status_rows[row] += 1

    lines = [f"扫描目录: {directory.resolve()}", f"JSON 文件: {len(files)}", ""]
    for title, rows in (("评论统计相关字段", count_rows), ("状态/隐藏相关真实字段", status_rows)):
        lines.extend([f"===== {title} =====", "字段名\tJSON path\t值\t出现次数\t对应 comment_id"])
        if not rows:
            lines.append("（未发现）")
        for (field, json_path, value, comment_id), occurrences in sorted(rows.items()):
            lines.append(f"{field}\t{json_path}\t{value}\t{occurrences}\t{comment_id}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描小黑盒评论 API 原始 JSON 的计数与状态字段")
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path("debug/raw/187482803"),
        help="包含 link_tree/sub_comments JSON 的目录",
    )
    parser.add_argument("-o", "--output", type=Path, help="同时把结果写入文本文件")
    args = parser.parse_args()
    report = analyze(args.directory)
    print(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
