from __future__ import annotations

from collections import OrderedDict
from typing import Any

from .api_client import LINK_TREE_PATH, SUB_COMMENTS_PATH
from .api_parser import parse_reply, result_comments
from .models import Comment, ExportData, Post, Reply, Statistics
from .utils import canonical_comment_id, int_or_none


def _append_source(item: Comment | Reply, source: dict[str, Any]) -> bool:
    """Add one provenance record and report whether it was new."""
    if source in item.sources:
        return False
    item.sources.append(source)
    return True


def _merge_author_fields(existing: Comment | Reply, incoming: Comment | Reply) -> None:
    for name in ("nickname", "uid", "avatar_url", "level"):
        if not getattr(existing.author, name) and getattr(incoming.author, name):
            setattr(existing.author, name, getattr(incoming.author, name))
    for badge in incoming.author.badges:
        if badge not in existing.author.badges:
            existing.author.badges.append(badge)


def _supplement(existing: Comment | Reply, incoming: Comment | Reply) -> None:
    """Fill missing presentation fields without replacing API ordering/data."""
    _merge_author_fields(existing, incoming)
    for name in ("content", "content_html", "created_at", "ip_location"):
        if not getattr(existing, name) and getattr(incoming, name):
            setattr(existing, name, getattr(incoming, name))
    if existing.likes is None and incoming.likes is not None:
        existing.likes = incoming.likes
    if not existing.images and incoming.images:
        existing.images = incoming.images
    existing.is_post_author = existing.is_post_author or incoming.is_post_author
    existing.is_deleted = existing.is_deleted or incoming.is_deleted
    existing.status_fields.update(incoming.status_fields)
    existing.raw.update(incoming.raw)


def _status_implies_hidden(status_fields: dict[str, Any]) -> bool:
    for key, value in status_fields.items():
        lowered = key.lower()
        normalized = str(value).strip().lower()
        truthy = value is True or normalized in {"1", "true", "yes", "hidden", "folded", "blocked"}
        falsey = value is False or normalized in {"0", "false", "no", "none", ""}
        if any(part in lowered for part in ("hide", "hidden", "fold", "meaningless", "shield", "ban", "blocked")) and truthy:
            return True
        if any(part in lowered for part in ("visible", "display", "can_show")) and falsey:
            return True
    return False


class CommentCollector:
    def __init__(self, post: Post):
        self.post = post
        self.comments: OrderedDict[str, Comment] = OrderedDict()
        self.duplicate_comments = 0
        self.duplicate_replies = 0
        self.expected_primary: int | None = None
        self.expected_total: int | None = post.displayed_comment_count
        self.api_reached_end = False
        self.child_api_reached_end: dict[str, bool] = {}
        self.child_request_traces: dict[str, list[dict[str, Any]]] = {}
        self.fetch_errors: list[str] = []
        self.result_metadata: dict[str, Any] = {}
        self.dom_merge_completed = False

    def merge_page(
        self,
        result: dict[str, Any],
        *,
        page: int | None = None,
        endpoint: str = LINK_TREE_PATH,
        is_last: bool = False,
        raw_file: str = "",
    ) -> tuple[int, int]:
        self.expected_primary = int_or_none(result.get("total_floor_num")) or self.expected_primary
        link = result.get("link") if isinstance(result.get("link"), dict) else {}
        self.expected_total = int_or_none(link.get("comment_num")) or self.expected_total
        self.result_metadata.update({
            key: value for key, value in result.items()
            if key not in {"comments", "link"}
        })
        new_comments = 0
        new_replies = 0
        for api_order, incoming in enumerate(result_comments(result), start=1):
            incoming.id = canonical_comment_id(incoming.id)
            incoming.root_comment_id = incoming.id
            incoming.api_present = True
            api_source = {
                "type": "api",
                "endpoint": endpoint,
                "page": page,
                "order": api_order,
            }
            if raw_file:
                api_source["raw_file"] = raw_file
            _append_source(incoming, api_source)
            if incoming.is_pinned:
                _append_source(incoming, {"type": "pinned"})
            for reply_order, reply in enumerate(incoming.replies, start=1):
                reply.id = canonical_comment_id(reply.id)
                reply.parent_comment_id = incoming.id
                reply.api_present = True
                child_source = dict(api_source)
                child_source["root_comment_id"] = incoming.id
                child_source["reply_order"] = reply_order
                _append_source(reply, child_source)

            existing = self.comments.get(incoming.id)
            if existing is None:
                self.comments[incoming.id] = incoming
                new_comments += 1
                new_replies += len(incoming.replies)
                continue

            source_was_new = _append_source(existing, api_source)
            if incoming.is_pinned:
                _append_source(existing, {"type": "pinned"})
            if source_was_new:
                self.duplicate_comments += 1
            existing.api_present = True
            existing.is_pinned = existing.is_pinned or incoming.is_pinned
            _supplement(existing, incoming)
            known = {reply.id: reply for reply in existing.replies}
            for reply in incoming.replies:
                current = known.get(reply.id)
                if current is None:
                    existing.replies.append(reply)
                    known[reply.id] = reply
                    new_replies += 1
                    continue
                had_new_source = False
                for source in reply.sources:
                    had_new_source = _append_source(current, source) or had_new_source
                if had_new_source:
                    self.duplicate_replies += 1
                current.api_present = True
                _supplement(current, reply)
        if is_last:
            self.api_reached_end = True
        return new_comments, new_replies

    def merge_child_page(
        self,
        root_id: str,
        result: dict[str, Any],
        *,
        lastval: str = "",
        request_index: int | None = None,
        raw_file: str = "",
    ) -> int:
        root_key = canonical_comment_id(root_id)
        comments = result.get("comments") if isinstance(result.get("comments"), list) else []
        trace = {
            "request": request_index or len(self.child_request_traces.get(root_key, [])) + 1,
            "endpoint": SUB_COMMENTS_PATH,
            "lastval": str(lastval or ""),
            "result_lastval": str(result.get("lastval") or ""),
            "has_more": bool(result.get("has_more")),
            "returned": len(comments),
        }
        if raw_file:
            trace["raw_file"] = raw_file
        self.child_request_traces.setdefault(root_key, []).append(trace)
        self.child_api_reached_end[root_key] = not bool(result.get("has_more"))

        root = self.comments.get(root_key)
        if root is None:
            return 0
        known = {reply.id: reply for reply in root.replies}
        added = 0
        for api_order, raw in enumerate(comments, start=1):
            if not isinstance(raw, dict):
                continue
            reply = parse_reply(raw, root)
            reply.id = canonical_comment_id(reply.id)
            reply.parent_comment_id = root_key
            reply.api_present = True
            source = {
                "type": "api",
                "endpoint": SUB_COMMENTS_PATH,
                "root_comment_id": root_key,
                "request": trace["request"],
                "lastval": trace["lastval"],
                "order": api_order,
            }
            if raw_file:
                source["raw_file"] = raw_file
            _append_source(reply, source)
            current = known.get(reply.id)
            if current is not None:
                if _append_source(current, source):
                    self.duplicate_replies += 1
                current.api_present = True
                _supplement(current, reply)
                continue
            root.replies.append(reply)
            known[reply.id] = reply
            added += 1
        return added

    def mark_fetch_error(self, label: str) -> None:
        if label not in self.fetch_errors:
            self.fetch_errors.append(label)

    def merge_dom(self, comments: list[Comment], visibility: dict[str, bool] | None = None) -> None:
        """Use final DOM only as a supplement after API pagination has ended."""
        self.dom_merge_completed = True
        visibility = visibility or {}
        for incoming in comments:
            incoming.id = canonical_comment_id(incoming.id)
            real_id = bool(incoming.id and not incoming.id.startswith("fallback-"))
            if not real_id:
                continue
            visible = visibility.get(incoming.id, True)
            dom_source = {"type": "dom", "visible": visible}
            incoming.dom_present = True
            incoming.visibility = "visible" if visible else "folded"
            _append_source(incoming, dom_source)
            existing = self.comments.get(incoming.id)
            if existing is None:
                if not self.api_reached_end:
                    continue
                incoming.sources = [{"type": "dom_only", "visible": visible}]
                incoming.api_present = False
                incoming.dom_present = True
                self.comments[incoming.id] = incoming
                existing = incoming
            else:
                existing.dom_present = True
                if not visible:
                    existing.visibility = "folded"
                _append_source(existing, dom_source)
                _supplement(existing, incoming)

            known_replies = {reply.id: reply for reply in existing.replies}
            for reply in incoming.replies:
                reply.id = canonical_comment_id(reply.id)
                real_reply_id = bool(reply.id and not reply.id.startswith("fallback-"))
                if not real_reply_id:
                    continue
                reply.dom_present = True
                reply_source = {"type": "dom", "root_comment_id": existing.id}
                _append_source(reply, reply_source)
                current = known_replies.get(reply.id)
                if current is None:
                    reply.sources = [{"type": "dom_only", "root_comment_id": existing.id}]
                    existing.replies.append(reply)
                    known_replies[reply.id] = reply
                else:
                    current.dom_present = True
                    _append_source(current, reply_source)
                    _supplement(current, reply)

    def build(self) -> ExportData:
        comments = list(self.comments.values())
        for comment in comments:
            if comment.is_deleted:
                comment.visibility = "deleted"
            elif (
                comment.api_present
                and not comment.dom_present
                and (self.dom_merge_completed or _status_implies_hidden(comment.status_fields))
            ):
                comment.visibility = "api_visible_hidden"
            for reply in comment.replies:
                if reply.is_deleted:
                    reply.visibility = "deleted"
                elif reply.api_present and not reply.dom_present and _status_implies_hidden(reply.status_fields):
                    reply.visibility = "api_visible_hidden"
        replies = sum(len(item.replies) for item in comments)
        incomplete: list[str] = []
        counted_not_returned: dict[str, int] = {}
        for item in comments:
            expected = item.expected_reply_count
            if expected is None or len(item.replies) >= expected:
                continue
            gap = expected - len(item.replies)
            initial_ended = not bool(item.raw.get("has_more"))
            child_ended = self.child_api_reached_end.get(item.id, initial_ended)
            if child_ended:
                counted_not_returned[item.id] = gap
            else:
                incomplete.append(item.id)

        pinned = sum(1 for item in comments if item.is_pinned)
        api_count = sum(1 for item in comments if item.api_present)
        dom_count = sum(1 for item in comments if item.dom_present)
        overlap = sum(1 for item in comments if item.api_present and item.dom_present)
        actual_total = len(comments) + replies
        expected_total = int_or_none(self.expected_total)
        discrepancy = (expected_total - actual_total) if expected_total is not None else 0
        stats = Statistics(
            expected_primary_comments=int_or_none(self.expected_primary),
            expected_total_comments=expected_total,
            primary_comments=len(comments),
            replies=replies,
            total_comments=actual_total,
            duplicate_primary_comments=self.duplicate_comments,
            duplicate_replies=self.duplicate_replies,
            incomplete_reply_roots=incomplete,
            counted_but_not_returned_roots=counted_not_returned,
            pinned_primary_comments=pinned,
            api_primary_comments=api_count,
            dom_primary_comments=dom_count,
            api_dom_overlap=overlap,
            api_only_comments=sum(1 for item in comments if item.api_present and not item.dom_present),
            dom_only_comments=sum(1 for item in comments if item.dom_present and not item.api_present),
            server_unavailable_comments=sum(counted_not_returned.values()),
            count_discrepancy=discrepancy,
            api_reached_end=self.api_reached_end,
        )

        unfinished = not self.api_reached_end or bool(incomplete) or bool(self.fetch_errors)
        stats.completeness = "partial" if unfinished else "complete_visible"
        ordinary_primary = stats.primary_comments - stats.pinned_primary_comments
        if stats.expected_primary_comments is not None and ordinary_primary != stats.expected_primary_comments:
            stats.notes.append(
                f"官方显示一级评论 {stats.expected_primary_comments} 条；可读取普通一级评论 {ordinary_primary} 条，"
                f"另有置顶 {stats.pinned_primary_comments} 条。"
            )
        elif stats.pinned_primary_comments:
            stats.notes.append(
                f"一级评论统计不含置顶特殊返回：普通 {ordinary_primary} 条 + 置顶 {stats.pinned_primary_comments} 条。"
            )
        if discrepancy:
            stats.notes.append(f"官方评论合计与可读取唯一评论相差 {discrepancy} 条。")
        if counted_not_returned:
            detail = "，".join(f"{root_id}: {gap}" for root_id, gap in counted_not_returned.items())
            stats.notes.append(f"楼中楼分页已结束但 child_num 仍有缺口（{detail}）；服务端未返回 ID/正文。")
        if incomplete:
            stats.notes.append(f"仍有 {len(incomplete)} 个楼中楼分页没有明确结束。")
        if self.fetch_errors:
            stats.notes.append("请求/交互未完成：" + "，".join(self.fetch_errors))
        return ExportData(
            post=self.post,
            comments=comments,
            statistics=stats,
            source={
                "mode": "playwright_api",
                "comment_result_metadata": self.result_metadata,
                "child_requests": self.child_request_traces,
                "fetch_errors": self.fetch_errors,
            },
        )
