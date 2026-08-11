from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Author:
    nickname: str = ""
    uid: str = ""
    avatar_url: str = ""
    badges: list[str] = field(default_factory=list)
    level: str = ""


@dataclass
class Media:
    url: str
    kind: str = "image"
    local_path: str = ""
    alt: str = ""


@dataclass
class Reply:
    id: str = ""
    parent_comment_id: str = ""
    reply_to_id: str = ""
    reply_to_name: str = ""
    author: Author = field(default_factory=Author)
    content: str = ""
    content_html: str = ""
    images: list[Media] = field(default_factory=list)
    created_at: str = ""
    ip_location: str = ""
    likes: int | None = None
    is_post_author: bool = False
    is_deleted: bool = False
    visibility: str = "visible"
    api_present: bool = False
    dom_present: bool = False
    sources: list[dict[str, Any]] = field(default_factory=list)
    status_fields: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class Comment:
    id: str = ""
    floor: int | None = None
    author: Author = field(default_factory=Author)
    content: str = ""
    content_html: str = ""
    images: list[Media] = field(default_factory=list)
    created_at: str = ""
    ip_location: str = ""
    likes: int | None = None
    is_post_author: bool = False
    is_deleted: bool = False
    is_pinned: bool = False
    root_comment_id: str = ""
    parent_comment_id: str = ""
    expected_reply_count: int | None = None
    replies: list[Reply] = field(default_factory=list)
    visibility: str = "visible"
    api_present: bool = False
    dom_present: bool = False
    sources: list[dict[str, Any]] = field(default_factory=list)
    status_fields: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class Post:
    id: str = ""
    source_url: str = ""
    title: str = "未命名帖子"
    author: Author = field(default_factory=Author)
    created_at: str = ""
    edited_at: str = ""
    ip_location: str = ""
    community: str = ""
    tags: list[str] = field(default_factory=list)
    content: str = ""
    content_html: str = ""
    images: list[Media] = field(default_factory=list)
    videos: list[Media] = field(default_factory=list)
    external_links: list[str] = field(default_factory=list)
    likes: int | None = None
    favourites: int | None = None
    displayed_comment_count: int | None = None
    displayed_floor_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class Statistics:
    expected_primary_comments: int | None = None
    expected_total_comments: int | None = None
    primary_comments: int = 0
    replies: int = 0
    total_comments: int = 0
    duplicate_primary_comments: int = 0
    duplicate_replies: int = 0
    incomplete_reply_roots: list[str] = field(default_factory=list)
    counted_but_not_returned_roots: dict[str, int] = field(default_factory=dict)
    pinned_primary_comments: int = 0
    api_primary_comments: int = 0
    dom_primary_comments: int = 0
    api_dom_overlap: int = 0
    api_only_comments: int = 0
    dom_only_comments: int = 0
    server_unavailable_comments: int = 0
    count_discrepancy: int = 0
    api_reached_end: bool = False
    completeness: str = "unknown"
    notes: list[str] = field(default_factory=list)


@dataclass
class ExportData:
    post: Post
    comments: list[Comment] = field(default_factory=list)
    statistics: Statistics = field(default_factory=Statistics)
    source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["replies"] = [
            asdict(reply) for comment in self.comments for reply in comment.replies
        ]
        payload["media"] = {
            "post": [asdict(item) for item in self.post.images + self.post.videos],
            "comments": [asdict(media) for comment in self.comments for media in comment.images],
            "replies": [
                asdict(media)
                for comment in self.comments
                for reply in comment.replies
                for media in reply.images
            ],
        }
        payload["completeness"] = self.statistics.completeness
        payload["diagnostics"] = {
            "statistics_notes": list(self.statistics.notes),
            "source": dict(self.source),
        }
        return payload
