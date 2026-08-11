from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse


class InvalidXiaoheiheUrl(ValueError):
    pass


@dataclass(frozen=True)
class ParsedPostUrl:
    original_url: str
    link_id: str

    @property
    def canonical_url(self) -> str:
        return f"https://www.xiaoheihe.cn/app/bbs/link/{self.link_id}"


_PATH_PATTERNS = (
    re.compile(r"/app/bbs/link/([0-9A-Za-z_-]+)", re.I),
    re.compile(r"/bbs/link/([0-9A-Za-z_-]+)", re.I),
)
_TEXT_LINK_ID = re.compile(r"(?:link_id|linkid)[=\"':%\s]+([0-9A-Za-z_-]+)", re.I)


def _search_nested(value: str) -> str | None:
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
        match = _TEXT_LINK_ID.search(decoded)
        if match:
            return match.group(1)
        try:
            obj = json.loads(decoded)
        except (ValueError, TypeError):
            continue
        stack = [obj]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, item in current.items():
                    if key.lower() in {"link_id", "linkid"} and item is not None:
                        return str(item)
                    stack.append(item)
            elif isinstance(current, list):
                stack.extend(current)
    return None


def parse_post_url(value: str) -> ParsedPostUrl:
    original = (value or "").strip()
    if not original:
        raise InvalidXiaoheiheUrl("请输入小黑盒帖子链接")
    if "://" not in original:
        original = "https://" + original
    parsed = urlparse(original)
    host = parsed.hostname.lower() if parsed.hostname else ""
    if not (host == "xiaoheihe.cn" or host.endswith(".xiaoheihe.cn")):
        raise InvalidXiaoheiheUrl("这不是小黑盒域名的链接")

    for pattern in _PATH_PATTERNS:
        match = pattern.search(parsed.path)
        if match:
            return ParsedPostUrl(value, match.group(1))

    query = parse_qs(parsed.query)
    for key in ("link_id", "linkid"):
        if query.get(key) and query[key][0]:
            return ParsedPostUrl(value, query[key][0])
    for values in query.values():
        for nested in values:
            link_id = _search_nested(nested)
            if link_id:
                return ParsedPostUrl(value, link_id)

    match = _TEXT_LINK_ID.search(original)
    if match:
        return ParsedPostUrl(value, match.group(1))
    raise InvalidXiaoheiheUrl("链接中没有找到帖子 ID / link_id")

