from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit


LINK_TREE_PATH = "/bbs/app/link/tree"
SUB_COMMENTS_PATH = "/bbs/app/comment/sub/comments"


@dataclass(frozen=True)
class ApiResponseInfo:
    path: str
    page: int | None = None
    root_comment_id: str = ""
    lastval: str = ""


@dataclass(frozen=True)
class RequestKey:
    """Semantic identity for one useful Heybox comment API result."""

    path: str
    link_id: str = ""
    page: int | None = None
    root_comment_id: str = ""
    lastval: str = ""


def classify_api_url(url: str) -> ApiResponseInfo | None:
    parsed = urlsplit(url)
    if parsed.path not in {LINK_TREE_PATH, SUB_COMMENTS_PATH}:
        return None
    query = parse_qs(parsed.query)
    page = None
    try:
        page = int(query.get("page", [""])[0])
    except ValueError:
        pass
    return ApiResponseInfo(
        path=parsed.path,
        page=page,
        root_comment_id=query.get("root_comment_id", [""])[0],
        lastval=query.get("lastval", [""])[0],
    )


def request_key_for_url(url: str) -> RequestKey | None:
    info = classify_api_url(url)
    if info is None:
        return None
    query = parse_qs(urlsplit(url).query)
    return RequestKey(
        path=info.path,
        link_id=str(query.get("link_id", [""])[0]),
        page=info.page,
        root_comment_id=info.root_comment_id,
        lastval=info.lastval,
    )
