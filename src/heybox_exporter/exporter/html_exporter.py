from __future__ import annotations

import html
import re
from pathlib import Path

from ..models import Author, ExportData, Media


URL_RE = re.compile(r"(https?://[^\s<]+)")


def _text(value: str) -> str:
    escaped = html.escape(value or "")
    escaped = URL_RE.sub(r'<a href="\1" target="_blank" rel="noreferrer">\1</a>', escaped)
    return escaped


def _media(items: list[Media]) -> str:
    result = []
    for item in items:
        source = html.escape(item.local_path or item.url, quote=True)
        alt = html.escape(item.alt, quote=True)
        result.append(f'<a class="media" href="{source}" target="_blank"><img src="{source}" alt="{alt}" loading="lazy"></a>')
    return "".join(result)


def _author(author: Author) -> str:
    badges = "".join(f'<span class="badge">{html.escape(item)}</span>' for item in author.badges)
    return f'<span class="name">{html.escape(author.nickname or "未知用户")}</span>{badges}'


def render_html(data: ExportData) -> str:
    post, stats = data.post, data.statistics
    comments = []
    for index, comment in enumerate(data.comments, start=1):
        floor = comment.floor if comment.floor is not None else index
        flags = []
        if comment.is_pinned:
            flags.append('<span class="badge pinned">置顶</span>')
        if comment.is_post_author:
            flags.append('<span class="badge owner">楼主</span>')
        replies = []
        for reply in comment.replies:
            target = f' 回复 <strong>{html.escape(reply.reply_to_name)}</strong>' if reply.reply_to_name else ""
            reply_meta = " · ".join(filter(None, [reply.created_at, f"点赞 {reply.likes}" if reply.likes is not None else "", reply.ip_location]))
            replies.append(f'''<article class="reply">
              <header>{_author(reply.author)}{target}{'<span class="badge owner">楼主</span>' if reply.is_post_author else ''}</header>
              <div class="meta">{html.escape(reply_meta)}</div>
              <div class="body">{_text(reply.content or '（该回复已删除）')}</div>{_media(reply.images)}
            </article>''')
        meta = " · ".join(filter(None, [comment.created_at, f"点赞 {comment.likes}" if comment.likes is not None else "", f"UID {comment.author.uid}" if comment.author.uid else "", comment.ip_location, f"ID {comment.id}" if comment.id else ""]))
        comments.append(f'''<article class="comment">
          <header class="comment-head"><div><span class="floor">{floor} 楼</span>{_author(comment.author)}{''.join(flags)}</div><div class="meta">{html.escape(meta)}</div></header>
          <div class="body">{_text(comment.content or '（该评论已删除）')}</div>{_media(comment.images)}
          {f'<section class="replies">{"".join(replies)}</section>' if replies else ''}
        </article>''')
    post_meta = " · ".join(filter(None, [post.author.nickname, f"UID {post.author.uid}" if post.author.uid else "", post.created_at, post.ip_location]))
    counts = f"已获取 {stats.primary_comments} 条一级评论 / {stats.replies} 条回复（合计 {stats.total_comments}）"
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(post.title)}</title><style>
:root{{--bg:#eef1f4;--card:#fff;--text:#20252b;--muted:#68727d;--line:#dfe4e8;--accent:#246bfd;--soft:#f6f8fa}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font-family:"Microsoft YaHei UI","PingFang SC",system-ui,sans-serif;line-height:1.78}}
main{{max-width:1000px;margin:36px auto;padding:0 20px 80px}} a{{color:#165dcc;text-decoration:none}} a:hover{{text-decoration:underline}}
.post,.comment{{background:var(--card);border:1px solid var(--line);border-radius:13px;box-shadow:0 3px 14px #1f293708}}
.post{{padding:38px 46px;margin-bottom:30px}} h1{{font-size:30px;line-height:1.35;margin:0 0 12px}} h2{{font-size:24px;margin:34px 0 16px}}
.meta{{color:var(--muted);font-size:13px}} .post-meta{{padding-bottom:24px;border-bottom:1px solid var(--line)}} .body{{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:20px}}
.tags{{margin-top:18px}} .badge{{display:inline-block;margin-left:7px;padding:1px 7px;border-radius:999px;background:#edf1f5;color:#5e6975;font-size:12px;vertical-align:2px}}
.badge.owner{{background:#fff1dc;color:#9a5b00}} .badge.pinned{{background:#e8efff;color:#245ac7}} .media{{display:block;margin-top:18px}}
.media img{{display:block;max-width:100%;height:auto;border-radius:9px;border:1px solid var(--line)}} .summary{{color:var(--muted);margin-bottom:16px}}
.comment{{padding:24px 28px;margin-bottom:16px}} .comment-head{{display:flex;justify-content:space-between;gap:20px;border-bottom:1px solid var(--line);padding-bottom:14px}}
.floor{{font-weight:700;margin-right:10px}} .name{{font-weight:650}} .replies{{margin:22px 0 0 32px;border-left:3px solid #dce5ef;background:var(--soft);border-radius:0 9px 9px 0;padding:3px 18px}}
.reply{{padding:15px 0;border-bottom:1px solid var(--line)}} .reply:last-child{{border-bottom:0}} .reply .body{{margin-top:7px}}
.warning{{background:#fff8e8;border:1px solid #eedcae;color:#75530b;padding:10px 14px;border-radius:8px;margin:12px 0}}
@media(max-width:700px){{main{{margin:15px auto;padding:0 10px 40px}}.post{{padding:25px 20px}}.comment{{padding:20px 18px}}.comment-head{{display:block}}.replies{{margin-left:10px}}h1{{font-size:25px}}}}
@media(prefers-color-scheme:dark){{:root{{--bg:#171a1e;--card:#22262b;--text:#e8ebee;--muted:#a8b0b8;--line:#373d44;--soft:#292e34;--accent:#75a1ff}}a{{color:#8cb3ff}}}}
</style></head><body><main>
<article class="post"><h1>{html.escape(post.title)}</h1><div class="meta post-meta">{html.escape(post_meta)}<br><a href="{html.escape(post.source_url, quote=True)}">原帖链接</a>{' · 点赞 '+str(post.likes) if post.likes is not None else ''}{' · 收藏 '+str(post.favourites) if post.favourites is not None else ''}</div>
<div class="body">{_text(post.content or '（正文为空或已删除）')}</div>{_media(post.images)}
<div class="tags">{''.join(f'<span class="badge">{html.escape(tag)}</span>' for tag in post.tags)}</div></article>
<h2>评论</h2><div class="summary">{counts} · 完整性：{html.escape(stats.completeness)}</div>
{''.join(f'<div class="warning">{html.escape(note)}</div>' for note in stats.notes)}
{''.join(comments)}
</main></body></html>'''


def export_html(data: ExportData, path: Path) -> None:
    path.write_text(render_html(data), encoding="utf-8")

