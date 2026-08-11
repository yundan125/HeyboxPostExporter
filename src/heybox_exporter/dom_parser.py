from __future__ import annotations

import copy
import re
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from .mhtml import MhtmlDocument, load_mhtml
from .models import Author, Comment, ExportData, Media, Post, Reply, Statistics
from .url_parser import parse_post_url
from .utils import canonical_comment_id, clean_ip, int_or_none, stable_fallback_id


PROFILE_ID = re.compile(r"/app/user/profile/([0-9A-Za-z_-]+)", re.I)
TOTAL_COUNT = re.compile(r"已有\s*([\d,]+)\s*条评论")
REPLY_COUNT = re.compile(r"([\d,]+)\s*条回复")


def _first_text(node: Tag | BeautifulSoup, selector: str) -> str:
    found = node.select_one(selector)
    return found.get_text(" ", strip=True) if found else ""


def _profile_uid(node: Tag | BeautifulSoup, selector: str = 'a[href*="/app/user/profile/"]') -> str:
    link = node.select_one(selector)
    match = PROFILE_ID.search(str(link.get("href") or "")) if link else None
    return match.group(1) if match else ""


def _content_text(node: Tag | None) -> str:
    if not node:
        return ""
    clone = copy.deepcopy(node)
    for emoji in clone.select("[data-emoji]"):
        label = str(emoji.get("data-emoji") or "").removeprefix("cube_")
        emoji.replace_with(f"[{label}]")
    for br in clone.find_all("br"):
        br.replace_with("\n")
    text = clone.get_text("", strip=False).replace("\xa0", " ")
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def _media(node: Tag | None) -> list[Media]:
    if not node:
        return []
    result: list[Media] = []
    seen: set[str] = set()
    for image in node.select("img[src]"):
        if "hb-avatar__image" in (image.get("class") or []):
            continue
        url = str(image.get("src") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        path = urlparse(url).path.lower()
        kind = "gif" if path.endswith(".gif") else "image"
        result.append(Media(url=url, kind=kind, alt=str(image.get("alt") or "")))
    return result


def _author(node: Tag, username_selector: str, profile_selector: str) -> Author:
    badges = [item.get_text(" ", strip=True) for item in node.select(".info-box__medal-item")]
    return Author(
        nickname=_first_text(node, username_selector),
        uid=_profile_uid(node, profile_selector),
        avatar_url=str((node.select_one(".hb-avatar__image") or {}).get("src") or ""),
        badges=[item for item in badges if item],
        level=_first_text(node, ".hb-level-tag__inner__text"),
    )


def parse_dom(html: str, source_url: str = "") -> ExportData:
    soup = BeautifulSoup(html, "lxml")
    article = soup.select_one(".hb-bbs-image-text, .hb-bbs-article, .hb-bbs-video")
    if not article:
        raise ValueError("未找到小黑盒帖子正文 DOM")

    title = _first_text(article, ".section-title__content") or _first_text(soup, "title") or "未命名帖子"
    user_section = article.select_one(".link-section-user") or article
    author = _author(user_section, ".link-user__username", '.link-user__user-wrapper[href*="/app/user/profile/"]')
    content_node = article.select_one(
        ".image-text__content, .article__content, .bbs-article__content, .bbs-video__content"
    )
    tag_names = [item.get_text(" ", strip=True) for item in article.select(".content-tag-text")]
    source_id = ""
    try:
        source_id = parse_post_url(source_url).link_id
    except ValueError:
        pass

    header_count = int_or_none(_first_text(soup, ".comment__comment-header .slide-tab__tab-cnt"))
    body_text = soup.get_text(" ", strip=True)
    total_match = TOTAL_COUNT.search(body_text)
    total_count = int_or_none(total_match.group(1)) if total_match else None
    operation_counts = [int_or_none(node.get_text(" ", strip=True)) for node in soup.select(".link-reply__operation-desc")]

    post = Post(
        id=source_id,
        source_url=source_url,
        title=title,
        author=author,
        created_at=_first_text(article, ".link-section-link-data .link-data__time")
        or _first_text(user_section, ".link-data__time"),
        ip_location=clean_ip(_first_text(article, ".link-section-link-data .link-data__ip")),
        community=tag_names[0] if tag_names else "",
        tags=tag_names,
        content=_content_text(content_node),
        content_html="".join(str(child) for child in content_node.contents) if content_node else "",
        images=_media(article.select_one(".image-text__header-image")) + _media(content_node),
        likes=operation_counts[0] if operation_counts else None,
        favourites=operation_counts[1] if len(operation_counts) > 1 else None,
        displayed_comment_count=total_count,
        displayed_floor_count=header_count,
    )

    comments: list[Comment] = []
    for index, item in enumerate(soup.select(".link-comment__list > .link-comment__comment-item"), start=1):
        comment_id = canonical_comment_id(item.get("data-comment-id"))
        content = item.select_one(":scope > .comment-item__content-container > .comment-item__content")
        comment_author = _author(
            item,
            ".comment-item-header__info-box .info-box__username",
            '.link-comment__comment-item-header a[href*="/app/user/profile/"]',
        )
        replies: list[Reply] = []
        for child in item.select(":scope .link-comment__comment-children > .comment-children-item"):
            creator = _first_text(child, ".children-item__comment-creator")
            reply_to_label = _first_text(child, ".children-item__reply-to")
            reply_to_name = ""
            if "回复" in reply_to_label:
                reply_to_name = reply_to_label.split("回复", 1)[1].rsplit(":", 1)[0].strip()
            child_content = child.select_one(".children-item__comment-content")
            reply_id = canonical_comment_id(child.get("data-comment-id"))
            child_author = Author(
                nickname=creator,
                uid=_profile_uid(child, '.children-item__comment-creator[href*="/app/user/profile/"]'),
                badges=[_first_text(child, ".children-item__writer-tag")] if child.select_one(".children-item__writer-tag") else [],
            )
            replies.append(
                Reply(
                    id=reply_id or stable_fallback_id(comment_id, creator, _first_text(child, ".children-item__create-time"), _content_text(child_content)),
                    parent_comment_id=comment_id,
                    reply_to_name=reply_to_name or comment_author.nickname,
                    author=child_author,
                    content=_content_text(child_content),
                    content_html="".join(str(part) for part in child_content.contents) if child_content else "",
                    images=_media(child),
                    created_at=_first_text(child, ".children-item__create-time"),
                    ip_location=clean_ip(_first_text(child, ".children-item__ip")),
                    is_post_author=bool(child.select_one(".children-item__writer-tag")),
                    dom_present=True,
                    sources=[{"type": "dom"}],
                    raw={"dom_comment_id": reply_id} if reply_id else {},
                )
            )
        load_label = _first_text(item, ".comment-children__load-all")
        expected = None
        count_match = REPLY_COUNT.search(load_label)
        if count_match:
            expected = int_or_none(count_match.group(1))
        elif item.select_one(".comment-children__load-all"):
            expected = len(replies) + 1
        elif replies:
            expected = len(replies)
        comment = Comment(
            id=comment_id or stable_fallback_id(comment_author.uid, _first_text(item, ".info-box__create-time"), _content_text(content), index),
            floor=index,
            author=comment_author,
            content=_content_text(content),
            content_html="".join(str(part) for part in content.contents) if content else "",
            images=_media(item.select_one(":scope > .comment-item__content-container")),
            created_at=_first_text(item, ".info-box__create-time"),
            ip_location=clean_ip(_first_text(item, ".info-box__ip")),
            likes=int_or_none(_first_text(item, ".like-box__cnt")),
            is_post_author=bool(item.select_one(".comment-item-header__writer-tag")) or comment_author.uid == post.author.uid,
            is_pinned="置顶" in item.get_text(" ", strip=True)[:80],
            expected_reply_count=expected,
            replies=replies,
            dom_present=True,
            sources=[{"type": "dom"}],
            raw={"dom_comment_id": comment_id} if comment_id else {},
        )
        comments.append(comment)

    stats = Statistics(
        expected_primary_comments=header_count,
        expected_total_comments=total_count,
        primary_comments=len(comments),
        replies=sum(len(item.replies) for item in comments),
    )
    stats.total_comments = stats.primary_comments + stats.replies
    stats.incomplete_reply_roots = [
        item.id for item in comments if item.expected_reply_count is not None and len(item.replies) < item.expected_reply_count
    ]
    stats.completeness = "complete" if (
        header_count == stats.primary_comments
        and (total_count is None or total_count == stats.total_comments)
        and not stats.incomplete_reply_roots
    ) else "partial"
    if stats.completeness == "partial":
        stats.notes.append("DOM/MHTML 快照只包含保存时已经加载和展开的评论。")
    return ExportData(post=post, comments=comments, statistics=stats, source={"mode": "dom"})


def parse_mhtml(path: str | Path) -> tuple[ExportData, MhtmlDocument]:
    document = load_mhtml(path)
    data = parse_dom(document.html, document.source_url)
    data.source.update({"mode": "mhtml", "path": str(Path(path).resolve())})
    return data, document
