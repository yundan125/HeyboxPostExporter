from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


WINDOWS_INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
SENSITIVE_QUERY_KEYS = {"hkey", "nonce", "token", "access_token", "pkey", "cookie", "authorization"}


def safe_filename(value: str, fallback: str = "未命名帖子", max_length: int = 100) -> str:
    name = WINDOWS_INVALID.sub("_", (value or "").strip()).rstrip(". ")
    name = re.sub(r"\s+", " ", name)[:max_length].rstrip(". ")
    return name or fallback


def unique_directory(parent: Path, name: str) -> Path:
    base = parent / safe_filename(name)
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{base.name}_{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def stable_fallback_id(*parts: object) -> str:
    text = "\x1f".join("" if item is None else str(item) for item in parts)
    return "fallback-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def canonical_comment_id(value: object) -> str:
    """Return the real Heybox comment id in one stable representation.

    API payloads and DOM adapters use ``commentid``, ``comment_id`` and ``id``
    for the same concept.  A real id always wins; fallback hashes are created by
    the caller only when this function returns an empty string.
    """
    if isinstance(value, dict):
        mapping: dict[str, Any] = value
        for key in ("commentid", "comment_id", "id"):
            candidate = mapping.get(key)
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()
        return ""
    if value is None:
        return ""
    return str(value).strip()


def int_or_none(value: object) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def clean_ip(value: str) -> str:
    return (value or "").strip().lstrip("·").strip()


def sanitize_url_for_log(url: str) -> str:
    from urllib.parse import parse_qsl, urlencode

    parsed = urlsplit(url)
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        query.append((key, "***" if key.lower() in SENSITIVE_QUERY_KEYS else value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
