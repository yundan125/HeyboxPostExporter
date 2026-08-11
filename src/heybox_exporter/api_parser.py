from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable

from .models import Author, Comment, Media, Post, Reply
from .utils import canonical_comment_id, clean_ip, int_or_none, stable_fallback_id


STATUS_FIELD_PARTS = (
    "hide", "hidden", "fold", "visible", "visibility", "display",
    "delete", "deleted", "audit", "review", "shield", "ban", "blocked",
    "status", "state", "meaningless",
)


def _timestamp(value: object) -> str:
    number = int_or_none(value)
    if number is None:
        return str(value or "")
    if number > 10_000_000_000:
        number //= 1000
    try:
        return datetime.fromtimestamp(number).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return str(value or "")


def _pick(mapping: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return default


def _author(raw: dict[str, Any]) -> Author:
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    level_info = user.get("level_info") if isinstance(user.get("level_info"), dict) else {}
    badges: list[str] = []
    for field in ("medals", "badges", "user_badges", "identity"):
        values = user.get(field) or raw.get(field) or []
        if isinstance(values, dict):
            values = [values]
        if not isinstance(values, list):
            values = [values]
        for item in values:
            if isinstance(item, dict):
                label = _pick(item, "name", "title", "text")
            else:
                label = str(item or "")
            if label and label not in badges:
                badges.append(label)
    return Author(
        nickname=str(_pick(user, "username", "nickname", "name", default=_pick(raw, "username", "nickname"))),
        uid=str(_pick(raw, "userid", "user_id", "uid", default=_pick(user, "userid", "user_id", "uid"))),
        avatar_url=str(_pick(user, "avatar", "avartar", "avatar_url", default=_pick(raw, "avatar", "avartar"))),
        badges=badges,
        level=str(_pick(level_info, "level", default=_pick(user, "level"))),
    )


def _media_items(value: object) -> list[Media]:
    if not value:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    result: list[Media] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            url, kind, alt = item, "image", ""
        elif isinstance(item, dict):
            url = str(_pick(item, "url", "src", "image_url", "original", "path"))
            kind = str(_pick(item, "type", "kind", default="image"))
            alt = str(_pick(item, "alt", "description", "text"))
        else:
            continue
        if url and url not in seen:
            seen.add(url)
            result.append(Media(url=url, kind=kind, alt=alt))
    return result


def _collect_rich_text(value: object) -> tuple[str, list[Media], list[str]]:
    if not value:
        return "", [], []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return value, [], []
        return _collect_rich_text(parsed)
    texts: list[str] = []
    media: list[Media] = []
    links: list[str] = []

    def walk(item: object) -> None:
        if isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, dict):
            if isinstance(item.get("text"), str):
                texts.append(item["text"])
            attrs = item.get("attrs") if isinstance(item.get("attrs"), dict) else {}
            kind = str(item.get("type") or "")
            url = _pick(attrs, "src", "url", "href", default=_pick(item, "src", "url", "href"))
            if url:
                if kind in {"image", "img", "gif"} or any(word in str(url).lower() for word in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
                    media.extend(_media_items({"url": url, "type": kind or "image", "alt": attrs.get("alt", "")}))
                else:
                    links.append(str(url))
            walk(item.get("content"))
            if kind in {"paragraph", "heading", "blockquote", "listItem"}:
                texts.append("\n")

    walk(value)
    text = "".join(texts)
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return text, media, list(dict.fromkeys(links))


def _comment_media(raw: dict[str, Any]) -> list[Media]:
    result: list[Media] = []
    for key in ("imgs", "images", "image_list", "attachments"):
        result.extend(_media_items(raw.get(key)))
    return list({item.url: item for item in result}.values())


def extract_status_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only status-like fields that really exist in a comment payload."""
    return {
        str(key): value
        for key, value in raw.items()
        if any(part in str(key).lower() for part in STATUS_FIELD_PARTS)
    }


def _is_deleted(raw: dict[str, Any]) -> bool:
    for key in ("is_deleted", "deleted"):
        if key in raw:
            value = raw.get(key)
            if value is True or bool(int_or_none(value)):
                return True
    status = str(raw.get("status") or raw.get("state") or "").lower()
    return status in {"deleted", "delete", "removed"}


def parse_reply(raw: dict[str, Any], parent: Comment) -> Reply:
    author = _author(raw)
    reply_user = raw.get("replyuser") if isinstance(raw.get("replyuser"), dict) else {}
    reply_to_name = str(_pick(reply_user, "username", "nickname", default=_pick(raw, "reply_username", "reply_name")))
    reply_id = canonical_comment_id(raw) or canonical_comment_id(raw.get("reply_id"))
    content = str(_pick(raw, "text", "content", "description"))
    deleted = _is_deleted(raw)
    if deleted and not content:
        content = "[该评论已被删除]"
    return Reply(
        id=reply_id or stable_fallback_id(parent.id, author.uid or author.nickname, raw.get("create_at"), content),
        parent_comment_id=parent.id,
        reply_to_id=str(_pick(raw, "replyid", "reply_id", "reply_comment_id")),
        reply_to_name=reply_to_name or parent.author.nickname,
        author=author,
        content=content,
        images=_comment_media(raw),
        created_at=_timestamp(_pick(raw, "create_at", "created_at", "time")),
        ip_location=clean_ip(str(_pick(raw, "ip_location", "ip"))),
        likes=int_or_none(_pick(raw, "up", "like_num", "likes", default=None)),
        is_post_author=bool(int_or_none(_pick(raw, "is_link_owner", "is_post_author", default=0))),
        is_deleted=deleted,
        status_fields=extract_status_fields(raw),
        raw=dict(raw),
    )


def parse_comment_wrapper(wrapper: dict[str, Any]) -> Comment | None:
    items = wrapper.get("comment") if isinstance(wrapper, dict) else None
    if isinstance(items, dict):
        items = items.get("comment") or [items]
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    raw = items[0]
    author = _author(raw)
    content = str(_pick(raw, "text", "content", "description"))
    comment_id = canonical_comment_id(raw)
    deleted = _is_deleted(raw)
    if deleted and not content:
        content = "[该评论已被删除]"
    comment = Comment(
        id=comment_id or stable_fallback_id(author.uid or author.nickname, raw.get("create_at"), content, raw.get("floor_num"), "root"),
        floor=int_or_none(_pick(raw, "floor_num", "floor", "index", default=None)),
        author=author,
        content=content,
        images=_comment_media(raw),
        created_at=_timestamp(_pick(raw, "create_at", "created_at", "time")),
        ip_location=clean_ip(str(_pick(raw, "ip_location", "ip"))),
        likes=int_or_none(_pick(raw, "up", "like_num", "likes", default=None)),
        is_post_author=bool(int_or_none(_pick(raw, "is_link_owner", "is_post_author", default=0))),
        is_deleted=deleted,
        is_pinned=bool(int_or_none(_pick(raw, "is_top", "is_pinned", default=0))),
        root_comment_id=comment_id,
        expected_reply_count=int_or_none(_pick(raw, "child_num", "reply_num", default=None)),
        status_fields=extract_status_fields(raw),
        raw=dict(raw),
    )
    comment.replies = [parse_reply(item, comment) for item in items[1:] if isinstance(item, dict)]
    return comment


def parse_post(raw: dict[str, Any], source_url: str, link_id: str) -> Post:
    author = _author(raw)
    plain, rich_media, links = _collect_rich_text(raw.get("text"))
    images = []
    for key in ("imgs", "images", "image_list"):
        images.extend(_media_items(raw.get(key)))
    images.extend(rich_media)
    videos = _media_items(raw.get("video_url") or raw.get("videos"))
    tags: list[str] = []
    for key in ("content_tags", "tags", "topics", "games"):
        value = raw.get(key) or []
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            value = [value]
        for item in value:
            label = str(_pick(item, "name", "title", "text") if isinstance(item, dict) else item)
            if label and label not in tags:
                tags.append(label)
    return Post(
        id=str(_pick(raw, "linkid", "link_id", default=link_id)),
        source_url=source_url,
        title=str(_pick(raw, "title", "subject", default="未命名帖子")),
        author=author,
        created_at=_timestamp(_pick(raw, "create_at", "created_at", "time")),
        edited_at=_timestamp(_pick(raw, "update_at", "edited_at", "last_edit_at")) if _pick(raw, "update_at", "edited_at", "last_edit_at") else "",
        ip_location=clean_ip(str(_pick(raw, "ip_location", "ip"))),
        community=str(_pick(raw, "community_name", "topic_name", default=tags[0] if tags else "")),
        tags=tags,
        content=plain or str(raw.get("text") or ""),
        images=list({item.url: item for item in images}.values()),
        videos=videos,
        external_links=links,
        likes=int_or_none(_pick(raw, "up", "support_count", "like_num", default=None)),
        favourites=int_or_none(_pick(raw, "favour_count", "favorite_count", default=None)),
        displayed_comment_count=int_or_none(_pick(raw, "comment_num", "comment_count", default=None)),
        raw=dict(raw),
    )


def result_comments(result: dict[str, Any]) -> Iterable[Comment]:
    for wrapper in result.get("comments") or []:
        if isinstance(wrapper, dict):
            comment = parse_comment_wrapper(wrapper)
            if comment:
                yield comment
