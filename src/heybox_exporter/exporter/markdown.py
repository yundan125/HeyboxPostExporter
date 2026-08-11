from __future__ import annotations

from pathlib import Path

from ..models import Author, ExportData, Media


def _meta(label: str, value: object) -> str:
    return f"> **{label}：** {value}\n" if value not in (None, "", []) else ""


def _author_name(author: Author) -> str:
    badge = f" · {' · '.join(author.badges)}" if author.badges else ""
    return f"{author.nickname or '未知用户'}{badge}"


def _media(items: list[Media]) -> str:
    blocks = []
    for item in items:
        source = item.local_path or item.url
        blocks.append(f"[![{item.alt}]({source})]({source})")
    return "\n\n".join(blocks)


def render_markdown(data: ExportData) -> str:
    post = data.post
    stats = data.statistics
    lines = [f"# {post.title}", ""]
    lines.append(_meta("作者", _author_name(post.author)).rstrip())
    lines.append(_meta("UID", post.author.uid).rstrip())
    lines.append(_meta("发布时间", post.created_at).rstrip())
    lines.append(_meta("最后编辑", post.edited_at).rstrip())
    lines.append(_meta("IP 属地", post.ip_location).rstrip())
    lines.append(_meta("所属社区", post.community).rstrip())
    lines.append(_meta("标签", " / ".join(post.tags)).rstrip())
    lines.append(_meta("帖子链接", post.source_url).rstrip())
    lines.append(_meta("点赞", post.likes).rstrip())
    lines.append(_meta("收藏", post.favourites).rstrip())
    lines.append(_meta("评论", post.displayed_comment_count).rstrip())
    lines = [line for line in lines if line]
    lines.extend(["", "---", "", "## 原帖", "", post.content or "（正文为空或已删除）"])
    if post.images:
        lines.extend(["", "### 图片", "", _media(post.images)])
    if post.videos:
        lines.extend(["", "### 视频", ""])
        for video in post.videos:
            source = video.local_path or video.url
            lines.append(f"- [{source}]({source})")
    if post.external_links:
        lines.extend(["", "### 外部链接", ""])
        lines.extend(f"- [{url}]({url})" for url in post.external_links)
    lines.extend([
        "", "---", "", "# 评论", "",
        f"共获取：{stats.primary_comments} 条一级评论 / {stats.replies} 条回复（合计 {stats.total_comments}）",
        "",
    ])
    if stats.expected_total_comments is not None:
        lines.append(f"> 页面/API 显示评论总数：{stats.expected_total_comments}；完整性：{stats.completeness}\n")
    for index, comment in enumerate(data.comments, start=1):
        floor = comment.floor if comment.floor is not None else index
        flags = []
        if comment.is_pinned:
            flags.append("置顶")
        if comment.is_post_author:
            flags.append("楼主")
        flag = f" · {' · '.join(flags)}" if flags else ""
        lines.extend(["---", "", f"## {floor} 楼 · {_author_name(comment.author)}{flag}", ""])
        if comment.created_at:
            lines.append(f"**时间：** {comment.created_at}")
        if comment.likes is not None:
            lines.append(f"**点赞：** {comment.likes}")
        if comment.author.uid:
            lines.append(f"**UID：** {comment.author.uid}")
        if comment.ip_location:
            lines.append(f"**IP 属地：** {comment.ip_location}")
        if comment.id:
            lines.append(f"**评论 ID：** {comment.id}")
        lines.extend(["", comment.content or "（该评论已删除）"])
        if comment.images:
            lines.extend(["", _media(comment.images)])
        for reply in comment.replies:
            target = f" 回复 {reply.reply_to_name}" if reply.reply_to_name else ""
            owner = " · 楼主" if reply.is_post_author else ""
            lines.extend(["", f"> **↳ {_author_name(reply.author)}{target}{owner}**", ">"])
            meta = []
            if reply.created_at:
                meta.append(f"时间：{reply.created_at}")
            if reply.likes is not None:
                meta.append(f"点赞：{reply.likes}")
            if reply.ip_location:
                meta.append(f"IP 属地：{reply.ip_location}")
            if meta:
                lines.append("> " + " · ".join(meta))
                lines.append(">")
            content_lines = (reply.content or "（该回复已删除）").splitlines() or [""]
            lines.extend("> " + line for line in content_lines)
            for media in reply.images:
                source = media.local_path or media.url
                lines.extend([">", f"> [![{media.alt}]({source})]({source})"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_markdown(data: ExportData, path: Path) -> None:
    path.write_text(render_markdown(data), encoding="utf-8")

