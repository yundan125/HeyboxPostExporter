from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .models import Comment, ExportData


def _source_types(comment: Comment) -> list[str]:
    result: list[str] = []
    for source in comment.sources:
        kind = str(source.get("type") or "")
        if kind and kind not in result:
            result.append(kind)
    return result


def _join_source_values(comment: Comment, key: str, *, api_only: bool = True) -> str:
    values: list[str] = []
    for source in comment.sources:
        if api_only and source.get("type") != "api":
            continue
        value = source.get(key)
        if value is not None and str(value) not in values:
            values.append(str(value))
    return ";".join(values)


def write_comment_diagnostics(data: ExportData, debug_dir: Path) -> tuple[Path, Path]:
    debug_dir.mkdir(parents=True, exist_ok=True)
    csv_path = debug_dir / "comment-diagnostics.csv"
    report_path = debug_dir / "comment-report.txt"
    fields = [
        "comment_id", "floor", "author_uid", "author_name", "root_comment_id",
        "parent_comment_id", "child_num", "is_pinned", "source", "api_page",
        "api_endpoint", "api_order", "dom", "api", "visibility", "status_fields",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for comment in data.comments:
            writer.writerow({
                "comment_id": comment.id,
                "floor": "" if comment.floor is None else comment.floor,
                "author_uid": comment.author.uid,
                "author_name": comment.author.nickname,
                "root_comment_id": comment.root_comment_id or comment.id,
                "parent_comment_id": comment.parent_comment_id,
                "child_num": "" if comment.expected_reply_count is None else comment.expected_reply_count,
                "is_pinned": str(comment.is_pinned).lower(),
                "source": "+".join(_source_types(comment)),
                "api_page": _join_source_values(comment, "page"),
                "api_endpoint": _join_source_values(comment, "endpoint"),
                "api_order": _join_source_values(comment, "order"),
                "dom": str(comment.dom_present).lower(),
                "api": str(comment.api_present).lower(),
                "visibility": comment.visibility,
                "status_fields": json.dumps(comment.status_fields, ensure_ascii=False, sort_keys=True),
            })
    report_path.write_text(build_comment_report(data), encoding="utf-8")
    return csv_path, report_path


def build_comment_report(data: ExportData) -> str:
    stats = data.statistics
    lines = [
        f"帖子 ID: {data.post.id}",
        "",
        "===== 官方统计 =====",
        f"一级评论: {stats.expected_primary_comments}",
        f"全部评论: {stats.expected_total_comments}",
        "",
        "===== 实际 API / 可读取数据 =====",
        f"唯一一级评论: {stats.primary_comments}",
        f"唯一回复: {stats.replies}",
        f"唯一评论合计: {stats.total_comments}",
        f"抓取状态: {stats.completeness}",
        f"服务端未返回: {stats.server_unavailable_comments}",
        "",
        "===== 一级评论来源 =====",
        f"link/tree: {stats.api_primary_comments}",
        f"DOM: {stats.dom_primary_comments}",
        f"Pinned: {stats.pinned_primary_comments}",
        f"API + DOM 重复: {stats.api_dom_overlap}",
        f"API Only: {stats.api_only_comments}",
        f"DOM Only: {stats.dom_only_comments}",
        f"分页/来源重复 ID: {stats.duplicate_primary_comments}",
        "",
        "===== 一级 comment_id（保持官方热度顺序） =====",
    ]
    for index, comment in enumerate(data.comments, start=1):
        source = "+".join(_source_types(comment))
        lines.append(
            f"{index}. {comment.id} | floor={comment.floor} | author={comment.author.nickname} "
            f"({comment.author.uid}) | source={source} | visibility={comment.visibility}"
        )

    special = [
        comment for comment in data.comments
        if comment.is_pinned or not comment.api_present or not comment.dom_present
        or comment.visibility != "visible"
    ]
    lines.extend(["", "===== 特殊评论 ====="])
    if not special:
        lines.append("无")
    for comment in special:
        lines.extend([
            f"Comment {comment.id}",
            f"author: {comment.author.nickname} ({comment.author.uid})",
            f"source: {'+'.join(_source_types(comment))}",
            f"DOM present: {comment.dom_present}",
            f"API present: {comment.api_present}",
            f"pinned: {comment.is_pinned}",
            f"visibility: {comment.visibility}",
            f"status fields: {json.dumps(comment.status_fields, ensure_ascii=False, sort_keys=True)}",
            "",
        ])

    child_requests = data.source.get("child_requests") if isinstance(data.source, dict) else {}
    child_requests = child_requests if isinstance(child_requests, dict) else {}
    lines.append("===== 楼中楼 =====")
    gap_roots = set(stats.counted_but_not_returned_roots) | set(stats.incomplete_reply_roots)
    if not gap_roots:
        lines.append("所有 child_num 与唯一可读取回复数一致。")
    for root_id in gap_roots:
        root = next((item for item in data.comments if item.id == root_id), None)
        if root is None:
            continue
        expected = root.expected_reply_count or 0
        actual = len(root.replies)
        lines.extend([
            f"Root {root_id}:",
            f"child_num = {expected}",
            f"API/DOM returned unique = {actual}",
            f"reply_ids = {', '.join(reply.id for reply in root.replies)}",
            f"difference = {expected - actual}",
        ])
        traces = child_requests.get(root_id) if isinstance(child_requests.get(root_id), list) else []
        for trace in traces:
            lines.append(
                "request {request}: lastval={lastval}, result_lastval={result_lastval}, "
                "has_more={has_more}, returned={returned}, raw_file={raw_file}".format(
                    request=trace.get("request"),
                    lastval=trace.get("lastval"),
                    result_lastval=trace.get("result_lastval"),
                    has_more=trace.get("has_more"),
                    returned=trace.get("returned"),
                    raw_file=trace.get("raw_file", ""),
                )
            )
        classification = (
            "counted_but_not_returned"
            if root_id in stats.counted_but_not_returned_roots else "partial"
        )
        lines.extend([f"classification = {classification}", ""])

    lines.extend(["===== 结论 =====", f"数据状态：{stats.completeness}"])
    ordinary = stats.primary_comments - stats.pinned_primary_comments
    if (
        stats.pinned_primary_comments
        and stats.expected_primary_comments == ordinary
        and stats.primary_comments == ordinary + stats.pinned_primary_comments
    ):
        pinned_ids = ", ".join(item.id for item in data.comments if item.is_pinned)
        lines.append(
            f"一级评论差值来自置顶特殊返回（{pinned_ids}）：官方一级统计 {ordinary} 不含置顶，"
            f"/link/tree 同时返回 {stats.pinned_primary_comments} 条置顶；不是重复 ID 或隐藏一级评论。"
        )
    if stats.server_unavailable_comments:
        lines.append(
            f"官方统计比可读取唯一评论多 {stats.server_unavailable_comments} 条。相关楼中楼 has_more 已结束，"
            "但服务端未返回 comment_id/content；只能归类为 counted_but_not_returned，无法判断其原文或精确状态。"
        )
    if stats.incomplete_reply_roots:
        lines.append("仍有分页未完成，因此本次结果是真正的 partial。")
    folded_tip = data.source.get("comment_result_metadata", {}).get("folded_comment_tips")
    if folded_tip:
        lines.append(f"接口真实字段 folded_comment_tips: {folded_tip}")
        lines.append("该提示只证明客户端支持折叠规则，不足以证明本帖缺口对应某条折叠评论。")
    return "\n".join(lines).rstrip() + "\n"
